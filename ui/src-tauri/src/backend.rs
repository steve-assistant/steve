//! The backend, owned by the app: started when the app starts, started again
//! if it dies, and ended with the app.
//!
//! A Task Scheduler keeper (`agent/service.py`) used to start the backend at
//! logon and every five minutes after, and the app's Quit left it running.
//! Three layers each watched the one below, because the backend had no
//! owner. The app is that owner now: the backend runs while the tray icon
//! does (docs/plan/04-runtime.md, section 7).
//!
//! **One kill-on-close job object** (spike S2, docs/plan/spikes/s2-packaging.md).
//! The supervisor starts suspended, goes into the job, and only then runs,
//! so everything it starts -- workers, signal-cli, Java -- is in the job
//! before its first instruction, with no race. When the app exits for any
//! reason, a crash included, Windows closes the job and ends the whole tree.
//! A clean Quit asks the supervisor to stop first, so its workers drain; the
//! job is the backstop, not the shutdown path.
//!
//! **Started again, but not forever.** A supervisor that exits, or that stops
//! answering its proof for two minutes, is started again: at most five times
//! in ten minutes. Then the app says "Steve's backend keeps stopping", with
//! the last lines of its log, rather than loop. The keeper learned the second
//! case: a supervisor alive and not answering is as down as one that exited,
//! and a watcher that only waits for an exit never sees it.
//!
//! **One started by hand is left alone.** A developer running
//! `python -m core.supervisor` has a backend the app did not start. If the run
//! file's backend proves itself when the app would start one, the app uses it
//! and starts none of its own, as the keeper did with a supervisor it found
//! answering. Once that one has stopped answering for two minutes, the app
//! starts its own.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
    JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{
    OpenThread, ResumeThread, CREATE_NO_WINDOW, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
};

/// How many deaths in how long mean it keeps stopping.
const DEATHS: usize = 5;
const WITHIN: Duration = Duration::from_secs(10 * 60);
/// A start may take this long before an unanswered proof counts against it:
/// the first worker's start can take a minute on a loaded machine.
const GRACE: Duration = Duration::from_secs(120);
const CHECK_EVERY: Duration = Duration::from_secs(30);
const STRIKES: u32 = 4;
/// How long a stopping supervisor has to drain its workers before the job
/// ends what is left.
pub const STOP_WAIT: Duration = Duration::from_secs(10);
/// The log is emptied in place past this, as the keeper did: the
/// supervisor holds it open as its output.
const MAX_LOG_BYTES: u64 = 5_000_000;
const TAIL_LINES: usize = 20;

/// A job object whose processes all end when its last handle closes.
pub struct Job(HANDLE);

// The handle is only ever used through the kernel, which serialises it.
unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Job {
    pub fn new() -> io::Result<Job> {
        // SAFETY: plain Win32 calls on a handle this function owns.
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let set = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if set == 0 {
                let err = io::Error::last_os_error();
                CloseHandle(handle);
                return Err(err);
            }
            Ok(Job(handle))
        }
    }

    fn assign(&self, child: &Child) -> io::Result<()> {
        // SAFETY: the child's handle is valid while `child` is.
        if unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    /// End everything in the job now. The job itself stays, for the next start.
    fn end_all(&self) {
        // SAFETY: a valid job handle.
        unsafe { TerminateJobObject(self.0, 1) };
    }

    /// How many processes are in the job now: a worker or signal-cli can
    /// outlive the supervisor that started it.
    fn running(&self) -> u32 {
        // SAFETY: a valid job handle, and a struct of the size passed.
        unsafe {
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = std::mem::zeroed();
            let asked = QueryInformationJobObject(
                self.0,
                JobObjectBasicAccountingInformation,
                &mut info as *mut _ as *mut core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            );
            if asked == 0 {
                // Not known is not empty: the caller ends the job and asks again.
                return u32::MAX;
            }
            info.ActiveProcesses
        }
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        // SAFETY: closing the handle this struct owns, once.
        unsafe { CloseHandle(self.0) };
    }
}

/// Run a process started with `CREATE_SUSPENDED`: resume its one thread.
fn resume(pid: u32) -> io::Result<()> {
    // SAFETY: a snapshot handle, and thread handles, each closed here.
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let mut entry: THREADENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
        let mut resumed = 0;
        let mut more = Thread32First(snapshot, &mut entry) != 0;
        while more {
            if entry.th32OwnerProcessID == pid {
                let thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                if !thread.is_null() {
                    if ResumeThread(thread) != u32::MAX {
                        resumed += 1;
                    }
                    CloseHandle(thread);
                }
            }
            more = Thread32Next(snapshot, &mut entry) != 0;
        }
        CloseHandle(snapshot);
        if resumed == 0 {
            return Err(io::Error::other(format!("no thread of process {pid} could be resumed")));
        }
    }
    Ok(())
}

/// Start `command` inside `job`: suspended, assigned, then resumed.
fn start_in(job: &Job, command: &mut Command) -> io::Result<Child> {
    let mut child = command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED).spawn()?;
    if let Err(err) = job.assign(&child).and_then(|()| resume(child.id())) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(err);
    }
    Ok(child)
}

/// Whether these deaths, the newest last, mean it keeps stopping.
fn keeps_stopping(deaths: &VecDeque<Instant>, now: Instant) -> bool {
    deaths.iter().filter(|died| now.duration_since(**died) <= WITHIN).count() >= DEATHS
}

/// The last `lines` lines of a file, read from its end.
fn tail(path: &Path, lines: usize) -> Vec<String> {
    let Ok(mut file) = File::open(path) else {
        return Vec::new();
    };
    let length = file.metadata().map(|m| m.len()).unwrap_or(0);
    if file.seek(SeekFrom::Start(length.saturating_sub(16 * 1024))).is_err() {
        return Vec::new();
    }
    let mut bytes = Vec::new();
    let _ = file.read_to_end(&mut bytes);
    let text = String::from_utf8_lossy(&bytes);
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].iter().map(|line| line.to_string()).collect()
}

/// What the app's window shows about the backend.
#[derive(serde::Serialize)]
pub struct Status {
    /// "running", "stopped" (by the user), "failing" (keeps stopping),
    /// "starting" or "external" (one the app did not start).
    state: &'static str,
    pid: Option<u32>,
    /// Starts after a death, in the last ten minutes.
    restarts: usize,
    /// The last lines of its log: when it keeps stopping, those it ended with.
    log_tail: Vec<String>,
    /// When the install's files are not the ones shipped: which, and how.
    damaged: Option<String>,
}

impl Status {
    /// Stopped for good, until the user acts: the tray says so
    /// (`lib.rs`, `TrayText`). Stopped by the user is not: they know.
    pub fn gone(&self) -> bool {
        matches!(self.state, "failing" | "damaged")
    }
}

#[derive(Default)]
struct Inner {
    child: Option<Child>,
    started: Option<Instant>,
    /// Whether the user wants it running: false after Stop, and while quitting.
    wanted: bool,
    /// Started by hand, not by the app.
    external: bool,
    deaths: VecDeque<Instant>,
    failing: Option<Vec<String>>,
    /// Its files are not the ones shipped (`integrity.rs`): never started
    /// again while the app runs, by the watcher or by Start.
    damaged: Option<String>,
    strikes: u32,
    checked: Option<Instant>,
}

/// The backend bundled beside the app (`backend\`), if this is an install.
///
/// Known by the marker the build writes into the code it ships
/// (`backend\app\installed.json`), the rule core/paths.py follows. Not by
/// the checkout the app was built from: that path is baked into every build,
/// and on the machine that built a release it still holds a `.venv`, so the
/// release run there would have started the developer's code, and kept its
/// data in the checkout.
pub fn bundled(app_dir: &Path) -> Option<PathBuf> {
    let backend = app_dir.join("backend");
    backend.join("app").join("installed.json").is_file().then_some(backend)
}

/// An install that has lost its marker: its backend half deleted by an
/// uninstall stopped midway, or a half-applied update. Known by the
/// uninstaller Tauri's installer puts beside the program, which a
/// developer's build never has (its folder can hold a `backend`). Taken for
/// a developer's build, it ran the build machine's `.venv`, and the user
/// read "could not start C:\…" about a folder not on their computer, where
/// the damaged page offers Repair.
pub fn unmarked(app_dir: &Path) -> bool {
    bundled(app_dir).is_none() && app_dir.join("uninstall.exe").is_file()
}

/// The checkout this app runs its backend from: the one its build named,
/// and only in a developer's build. An install has none, marked or not, and
/// a release build names none (lib.rs's `repo_root`).
pub fn checkout<'a>(app_dir: &Path, named: Option<&'a Path>) -> Option<&'a Path> {
    named.filter(|_| bundled(app_dir).is_none() && !unmarked(app_dir))
}

/// Why an app with no backend to run says its files are damaged: an install
/// that lost its marker, or a release build away from its install, which
/// names no checkout to run instead.
pub fn missing(app_dir: &Path) -> String {
    if unmarked(app_dir) {
        "the backend's marker (backend\\app\\installed.json) is gone: \
         an uninstall or an update stopped halfway"
            .to_string()
    } else {
        "the app has no backend beside it (backend\\app\\installed.json): \
         start Steve from where it was installed"
            .to_string()
    }
}

/// What starts the backend, and in which folder.
#[derive(Debug, PartialEq)]
pub struct Launch {
    pub python: PathBuf,
    pub folder: PathBuf,
    pub flags: &'static [&'static str],
}

/// An install's Python is cut off from the user's (`-I`): its code is on
/// its path through `steve.pth`, and nothing else should be.
const INSTALLED: &[&str] = &["-I", "-B"];

/// A developer's build: `-E -s`, as core/worker_handle.py starts a worker,
/// not `-I`. `-I` also takes the checkout off the path, and the venv's
/// editable install then serves the code of the checkout it was installed
/// from: a worktree sharing the main checkout's venv ran main's backend
/// under its own app (step 9's spikes, 17 October 2026).
const CHECKOUT: &[&str] = &["-E", "-s", "-B"];

impl Launch {
    /// An install runs its own Python, in the folder of its code, where
    /// `steve.pth` and the marker expect it; a developer's build runs the
    /// checkout's `.venv` in the checkout, or the Python it is given
    /// (`STEVE_PYTHON`): a test's, whose site-packages holds the test fences
    /// (tests/gate/fences.fenced_python). An install never takes one.
    pub fn find(app_dir: &Path, named: Option<&Path>, given: Option<PathBuf>) -> Launch {
        // Only a developer's build runs a checkout. An unmarked install, or
        // a release build with no backend beside it, is refused before it
        // starts (lib.rs); if anything ever started one, it would be the
        // Python beside it, never a checkout's.
        match checkout(app_dir, named) {
            Some(checkout) => Launch {
                python: given
                    .filter(|p| !p.as_os_str().is_empty())
                    .unwrap_or_else(|| checkout.join(".venv").join("Scripts").join("python.exe")),
                folder: checkout.to_path_buf(),
                flags: CHECKOUT,
            },
            None => {
                let backend = app_dir.join("backend");
                Launch { python: backend.join("python.exe"), folder: backend.join("app"), flags: INSTALLED }
            }
        }
    }
}

pub struct Backend {
    job: Job,
    python: PathBuf,
    folder: PathBuf,
    flags: &'static [&'static str],
    log: PathBuf,
    inner: Mutex<Inner>,
}

impl Backend {
    pub fn new(launch: Launch, data: PathBuf) -> io::Result<Arc<Backend>> {
        Ok(Arc::new(Backend {
            job: Job::new()?,
            python: launch.python,
            folder: launch.folder,
            flags: launch.flags,
            log: data.join("logs").join("supervisor.log"),
            inner: Mutex::new(Inner::default()),
        }))
    }

    fn spawn(&self, inner: &mut Inner) {
        let opened = self.log.parent().map(std::fs::create_dir_all).transpose().and_then(|_| {
            File::options().create(true).append(true).open(&self.log)
        });
        let result = opened.and_then(|log| {
            let err = log.try_clone()?;
            start_in(
                &self.job,
                Command::new(&self.python)
                    .args(self.flags)
                    .args(["-m", "core.supervisor"])
                    .current_dir(&self.folder)
                    .stdin(Stdio::null())
                    .stdout(log)
                    .stderr(err),
            )
        });
        match result {
            Ok(child) => {
                log::info!("backend started, pid {}", child.id());
                inner.child = Some(child);
                inner.started = Some(Instant::now());
                inner.strikes = 0;
                inner.checked = None;
            }
            Err(err) => {
                // Counted as a death, so a missing Python ends in "keeps
                // stopping" with the reason, not a start every second.
                log::error!("could not start the backend: {err}");
                inner.deaths.push_back(Instant::now());
                if keeps_stopping(&inner.deaths, Instant::now()) {
                    inner.failing =
                        Some(vec![format!("could not start {}: {err}", self.python.display())]);
                }
            }
        }
    }

    /// Start it, unless one is running: the app's own, or one started by hand.
    pub fn start(&self) {
        let mut inner = self.inner.lock().unwrap();
        if inner.damaged.is_some() {
            return;
        }
        inner.wanted = true;
        inner.failing = None;
        inner.deaths.clear();
        if inner.child.is_some() {
            return;
        }
        drop(inner);
        let external = crate::proven().is_ok();
        let mut inner = self.inner.lock().unwrap();
        inner.external = external;
        inner.strikes = 0;
        inner.checked = None;
        if !external && inner.child.is_none() {
            self.spawn(&mut inner);
        }
    }

    /// Stop it, and never start it again while the app runs: its files are
    /// not the ones shipped. Running them would run whatever replaced them.
    pub fn refuse(&self, why: String) {
        self.inner.lock().unwrap().damaged = Some(why);
        self.stop();
    }

    /// Ask the supervisor to stop, give it `STOP_WAIT` to drain its workers,
    /// then end whatever is left in the job. One started by hand is asked,
    /// and waited for, but it is not the app's to end.
    pub fn stop(&self) {
        let child = {
            let mut inner = self.inner.lock().unwrap();
            inner.wanted = false;
            inner.external = false;
            inner.child.take()
        };
        let asked = crate::proven().and_then(|run| {
            crate::post(run.port, "/_sup/stop", "{}", Some(&run.token)).map(|_| ())
        });
        if let Err(err) = &asked {
            log::warn!("could not ask the backend to stop: {err}");
        }
        let deadline = Instant::now() + STOP_WAIT;
        match child {
            Some(mut child) => {
                while Instant::now() < deadline && matches!(child.try_wait(), Ok(None)) {
                    std::thread::sleep(Duration::from_millis(100));
                }
                self.job.end_all();
                let _ = child.wait();
            }
            // Until it has gone: a start straight after would otherwise find
            // it still answering, and start nothing.
            None if asked.is_ok() => {
                while Instant::now() < deadline && crate::proven().is_ok() {
                    std::thread::sleep(Duration::from_millis(250));
                }
            }
            None => {}
        }
    }

    pub fn restart(&self) {
        self.stop();
        self.start();
    }

    /// Before an installer replaces the files: stop it as Quit does, so the
    /// workers drain, then wait until nothing is left in the job. The app's
    /// own exit would end the job too, but only as the installer starts:
    /// a Python still dying holds its files open, and the copy fails on
    /// them. True once the job is empty.
    pub fn stop_for_update(&self) -> bool {
        self.stop();
        let deadline = Instant::now() + STOP_WAIT;
        while self.job.running() > 0 {
            if Instant::now() > deadline {
                return false;
            }
            self.job.end_all();
            std::thread::sleep(Duration::from_millis(100));
        }
        true
    }

    pub fn status(&self) -> Status {
        let mut inner = self.inner.lock().unwrap();
        let now = Instant::now();
        let restarts =
            inner.deaths.iter().filter(|died| now.duration_since(**died) <= WITHIN).count();
        let pid = inner.child.as_ref().map(|child| child.id());
        let alive =
            inner.child.as_mut().is_some_and(|child| matches!(child.try_wait(), Ok(None)));
        let state = match (&inner.failing, inner.wanted, inner.external, alive) {
            _ if inner.damaged.is_some() => "damaged",
            (Some(_), ..) => "failing",
            (None, false, ..) => "stopped",
            (None, true, true, _) => "external",
            (None, true, false, true) => "running",
            (None, true, false, false) => "starting",
        };
        Status {
            state,
            pid,
            restarts,
            // Diagnostics shows the end of the log at any time; when it keeps
            // stopping, the lines kept from the moment it was found to.
            log_tail: inner.failing.clone().unwrap_or_else(|| tail(&self.log, TAIL_LINES)),
            damaged: inner.damaged.clone(),
        }
    }

    /// One look: start it again if it died, end it if it stopped answering,
    /// start the app's own if one started by hand has gone. Returns the log's
    /// last lines when it has just been found to keep stopping.
    fn tick(&self) -> Option<Vec<String>> {
        let mut inner = self.inner.lock().unwrap();
        if !inner.wanted || inner.failing.is_some() {
            return None;
        }
        let exited = if inner.external {
            None
        } else {
            match inner.child.as_mut().map(|child| child.try_wait()) {
                Some(Ok(Some(code))) => Some(format!("{code}")),
                Some(Err(err)) => Some(err.to_string()),
                Some(Ok(None)) => None,
                None => Some("not running".into()),
            }
        };
        if let Some(why) = exited {
            if inner.child.take().is_some() {
                log::warn!("the backend stopped: {why}");
                inner.deaths.push_back(Instant::now());
            }
            // Its workers too, if they outlived it.
            self.job.end_all();
            if keeps_stopping(&inner.deaths, Instant::now()) {
                let lines = tail(&self.log, TAIL_LINES);
                inner.failing = Some(lines.clone());
                return Some(lines);
            }
            self.spawn(&mut inner);
            return None;
        }

        // Alive, or started by hand: does it answer? One started by hand has
        // no exit the app can see, so without this a developer who stopped
        // theirs would leave the app showing it, with nothing running.
        let now = Instant::now();
        let due = (inner.external || inner.started.is_some_and(|at| now.duration_since(at) >= GRACE))
            && inner.checked.map_or(true, |at| now.duration_since(at) >= CHECK_EVERY);
        if !due {
            return None;
        }
        inner.checked = Some(now);
        drop(inner);
        let answered = crate::proven().is_ok();
        let mut inner = self.inner.lock().unwrap();
        inner.strikes = if answered { 0 } else { inner.strikes + 1 };
        if inner.strikes >= STRIKES && inner.external {
            log::warn!("the backend started by hand has gone; starting the app's own");
            inner.external = false;
            self.spawn(&mut inner);
        } else if inner.strikes >= STRIKES {
            log::warn!("the backend is running and has not answered {STRIKES} times; ending it");
            // Its exit is seen, and counted, at the next look.
            self.job.end_all();
        }
        None
    }

    fn trim_log(&self) {
        if std::fs::metadata(&self.log).is_ok_and(|m| m.len() > MAX_LOG_BYTES) {
            if let Ok(file) = File::options().write(true).open(&self.log) {
                let _ = file.set_len(0);
            }
        }
    }

    /// Watch it for as long as the app runs; `failing` is told the log's last
    /// lines once it keeps stopping.
    pub fn watch(self: Arc<Self>, failing: impl Fn(Vec<String>) + Send + 'static) {
        std::thread::spawn(move || {
            let mut trimmed = Instant::now();
            loop {
                std::thread::sleep(Duration::from_secs(1));
                if let Some(lines) = self.tick() {
                    failing(lines);
                }
                if trimmed.elapsed() >= Duration::from_secs(60) {
                    self.trim_log();
                    trimmed = Instant::now();
                }
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target").join("test-scratch");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(name);
        let _ = std::fs::remove_file(&path);
        path
    }

    fn wait_for_exit(child: &mut Child, within: Duration) -> bool {
        let deadline = Instant::now() + within;
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        false
    }

    /// The backstop: a crash of the app, or an exit without Quit, must not
    /// leave the backend and its workers running with nobody to stop them.
    #[test]
    fn closing_the_job_ends_what_was_started_in_it() {
        let job = Job::new().unwrap();
        let mut child = start_in(&job, &mut ping()).unwrap();
        assert!(matches!(child.try_wait(), Ok(None)), "it should be running");

        drop(job);

        assert!(wait_for_exit(&mut child, Duration::from_secs(10)), "it outlived the job");
    }

    /// Suspended until it is in the job: anything it started before that
    /// would be outside it, and live on after the app.
    #[test]
    fn a_process_runs_only_once_it_is_in_the_job() {
        let job = Job::new().unwrap();
        let marker = scratch("ran");
        // A bare name in its own folder: with the path in quotes, Rust
        // escapes them as \" for the C runtime, which cmd does not read.
        let mut child = Command::new("cmd")
            .args(["/c", "echo ran> ran"])
            .current_dir(marker.parent().unwrap())
            .creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED)
            .spawn()
            .unwrap();
        std::thread::sleep(Duration::from_millis(500));
        assert!(!marker.exists(), "it ran while suspended");

        job.assign(&child).unwrap();
        resume(child.id()).unwrap();

        assert!(wait_for_exit(&mut child, Duration::from_secs(10)));
        assert!(marker.exists(), "it never ran after being resumed");
    }

    fn ping() -> Command {
        let mut command = Command::new("cmd");
        command.args(["/c", "ping -n 60 127.0.0.1 >nul"]);
        command
    }

    /// A Stop that the supervisor did not finish in time, or a death: what is
    /// left of the tree goes, and the next start goes into the same job.
    #[test]
    fn ending_the_job_ends_its_processes_and_keeps_the_job_for_the_next_start() {
        let job = Job::new().unwrap();
        let mut first = start_in(&job, &mut ping()).unwrap();

        job.end_all();

        assert!(wait_for_exit(&mut first, Duration::from_secs(10)));
        let mut second = start_in(&job, &mut ping()).unwrap();
        assert!(matches!(second.try_wait(), Ok(None)));
        drop(job);
        assert!(wait_for_exit(&mut second, Duration::from_secs(10)));
    }

    #[test]
    fn five_deaths_in_ten_minutes_is_keeps_stopping_and_older_ones_do_not_count() {
        let now = Instant::now() + Duration::from_secs(3600);
        let recent: VecDeque<Instant> =
            (0..5).map(|i| now - Duration::from_secs(60 * i)).collect();
        assert!(keeps_stopping(&recent, now));

        let four: VecDeque<Instant> = recent.iter().take(4).copied().collect();
        assert!(!keeps_stopping(&four, now));

        let mut spread = four.clone();
        spread.push_front(now - Duration::from_secs(11 * 60));
        assert!(!keeps_stopping(&spread, now), "a death eleven minutes ago counted");
    }

    #[test]
    fn the_logs_tail_is_its_last_lines() {
        let path = scratch("log.txt");
        let lines: Vec<String> = (1..=50).map(|i| format!("line {i}")).collect();
        std::fs::write(&path, lines.join("\n")).unwrap();

        let last = tail(&path, 3);

        assert_eq!(last, vec!["line 48", "line 49", "line 50"]);
        assert!(tail(&scratch("missing.txt"), 3).is_empty());
    }

    /// The installer cannot replace a file a running Python holds open, and
    /// a worker can outlive the supervisor that started it.
    #[test]
    fn an_update_waits_until_nothing_is_left_in_the_job() {
        let (app, checkout) = install_and_checkout("update");
        let backend = Backend::new(Launch::find(&app, Some(&checkout), None), app.clone()).unwrap();
        let mut worker = start_in(&backend.job, &mut ping()).unwrap();
        assert_eq!(backend.job.running(), 1);

        assert!(backend.stop_for_update());

        assert_eq!(backend.job.running(), 0);
        assert!(wait_for_exit(&mut worker, Duration::from_secs(10)));
    }

    #[test]
    fn a_backend_refused_for_damaged_files_is_never_started_and_says_why() {
        let (app, checkout) = install_and_checkout("refused");
        let backend = Backend::new(Launch::find(&app, Some(&checkout), None), app.clone()).unwrap();
        backend.inner.lock().unwrap().damaged = Some("backend/python.exe is missing".into());

        backend.start();

        let inner = backend.inner.lock().unwrap();
        assert!(inner.child.is_none() && inner.deaths.is_empty() && !inner.wanted);
        drop(inner);
        let status = backend.status();
        assert_eq!(status.state, "damaged");
        assert_eq!(status.damaged.as_deref(), Some("backend/python.exe is missing"));
        assert!(status.gone(), "the tray says it has stopped");
    }

    /// The tray says "Steve has stopped" from these alone: a backend that
    /// keeps stopping, or damaged files. Stopped by the user, or starting
    /// again, it says nothing of the kind.
    #[test]
    fn only_a_failing_or_damaged_backend_is_gone() {
        let (app, checkout) = install_and_checkout("gone");
        let backend = Backend::new(Launch::find(&app, Some(&checkout), None), app.clone()).unwrap();
        assert!(!backend.status().gone(), "stopped by the user");
        backend.inner.lock().unwrap().failing = Some(vec!["Traceback".into()]);
        assert!(backend.status().gone(), "keeps stopping");
        backend.inner.lock().unwrap().failing = None;
        backend.inner.lock().unwrap().wanted = true;
        assert!(!backend.status().gone(), "starting again");
    }

    /// An install beside a checkout, as on the machine that built it.
    fn install_and_checkout(name: &str) -> (PathBuf, PathBuf) {
        let base = scratch(name);
        let _ = std::fs::remove_dir_all(&base);
        let (app, checkout) = (base.join("Programs").join("Steve"), base.join("checkout"));
        std::fs::create_dir_all(app.join("backend").join("app")).unwrap();
        std::fs::create_dir_all(checkout.join(".venv").join("Scripts")).unwrap();
        std::fs::write(checkout.join("pyproject.toml"), "").unwrap();
        (app, checkout)
    }

    /// Isolated, and deaf to a Python it is given: an install's code is the
    /// one its list of files was signed for.
    #[test]
    fn an_install_runs_its_bundled_python_in_the_folder_of_its_code() {
        let (app, checkout) = install_and_checkout("launch-installed");
        std::fs::write(app.join("backend").join("app").join("installed.json"), "{}").unwrap();

        assert_eq!(
            Launch::find(&app, Some(&checkout), Some(PathBuf::from(r"C:\elsewhere\python.exe"))),
            Launch {
                python: app.join("backend").join("python.exe"),
                folder: app.join("backend").join("app"),
                flags: &["-I", "-B"],
            }
        );
    }

    /// Not `-I`: with it, a worktree sharing the main checkout's venv ran
    /// the main checkout's backend.
    #[test]
    fn a_developers_build_runs_its_checkouts_venv_though_a_backend_folder_is_beside_it() {
        let (app, checkout) = install_and_checkout("launch-checkout");

        assert_eq!(
            Launch::find(&app, Some(&checkout), None),
            Launch {
                python: checkout.join(".venv").join("Scripts").join("python.exe"),
                folder: checkout.clone(),
                flags: &["-E", "-s", "-B"],
            }
        );
    }

    /// Its uninstaller says it was installed: without the marker it is
    /// damaged, and never runs the checkout the build was made from.
    #[test]
    fn an_install_without_its_marker_is_damaged_and_never_runs_a_checkouts_python() {
        let (app, checkout) = install_and_checkout("launch-unmarked");
        assert!(!unmarked(&app), "a developer's build has no uninstaller");
        std::fs::write(app.join("uninstall.exe"), "").unwrap();

        assert!(unmarked(&app));
        let launch = Launch::find(&app, Some(&checkout), None);
        assert_eq!(launch.python, app.join("backend").join("python.exe"));
        assert_eq!(launch.flags, &["-I", "-B"]);
    }

    /// A release build names no checkout (lib.rs's `repo_root`). Copied away
    /// from its install, it runs nothing and says its backend is missing;
    /// until step 11 it ran the checkout it was built in, if the computer
    /// had one there (the audit's F7).
    #[test]
    fn a_build_that_names_no_checkout_never_runs_one_and_says_its_backend_is_missing() {
        let (app, _checkout) = install_and_checkout("launch-unnamed");

        assert_eq!(checkout(&app, None), None);
        let launch = Launch::find(&app, None, Some(PathBuf::from(r"C:\elsewhere\python.exe")));
        assert_eq!(launch.python, app.join("backend").join("python.exe"));
        assert_eq!(launch.flags, &["-I", "-B"]);
        assert!(missing(&app).contains("no backend beside it"), "{}", missing(&app));
        std::fs::write(app.join("uninstall.exe"), "").unwrap();
        assert!(missing(&app).contains("marker"), "{}", missing(&app));
    }

    /// A test's Python carries the fences, and the backend it starts must
    /// run in it; an empty variable is no choice.
    #[test]
    fn a_developers_build_runs_the_python_it_is_given_in_its_checkout() {
        let (app, checkout) = install_and_checkout("launch-given");
        let fenced = PathBuf::from(r"C:\tests\fenced-python\Scripts\python.exe");

        let given = Launch::find(&app, Some(&checkout), Some(fenced.clone()));
        let empty = Launch::find(&app, Some(&checkout), Some(PathBuf::new()));

        assert_eq!((given.python, given.folder), (fenced, checkout.clone()));
        assert_eq!(empty.python, checkout.join(".venv").join("Scripts").join("python.exe"));
    }
}
