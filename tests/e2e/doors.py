"""Every guarantee, from every door: the table, and where each route sits on it.

A guarantee checked from one door says nothing of the others. The review
of steps 7 to 9 found three holes of this kind, each a guarantee tested
from the main window and broken from a door beside it:
- Talk from the tray's menu opened the microphone through the quick bar,
  and clicking the bar away hid it without closing the microphone
  (finding 3, tests/unit/ui/test_quickbar_screen.py);
- the quick bar's Yes took a card whole, items joined after the user had
  read it included (finding 4);
- signed out, a toast showed a card, a reminder or a briefing in full,
  because the shell hid toasts only when Windows was locked (finding 8).

So the product's guarantees run down the side of `TABLE`, its doors run
across, and each pair names the tests that check that guarantee through
that door, or says why the door cannot reach it. `ROUTES` places every
route of the supervisor and the worker by the guarantees it touches.
tests/gate/test_doors.py checks that every pair is filled, that every test
named exists, and that every route is placed: a door or a route added
later fails it until someone has looked at it against each guarantee.

A pair names the tests that go through its door. Where a door is only a
way into code that every door shares (the approvals, the ledger), the
pair names that code's test, and says so. A pair the product has not yet
decided is `Open`, with what is reachable, so the table never claims a
check that does not run.
"""

from __future__ import annotations

from dataclasses import dataclass

GUARANTEES = {
    "card": "Nothing goes out without a yes on a card that shows what and to whom.",
    "high_risk": "A high-risk yes takes the passphrase or Windows Hello, bound to the card.",
    "microphone": "The microphone opens only when asked, and closes on every way out.",
    "cap": "Spending stops at the cap.",
    "secrets": "Secrets stay where they belong.",
    "controls": "The user's own controls (the knobs, the passphrase, Hello, sign-in, "
                "setup, the switches) are reached only by the user.",
    "signed_out": "Signed out, nothing private shows and no yes is taken.",
}

DOORS = {
    "window": "the main window",
    "quick_bar": "the quick bar",
    "desk_card": "the desk card",
    "tray": "the tray's menu",
    "toast": "a toast",
    "signal": "Signal",
    "telegram": "Telegram",
    "push_to_talk": "push-to-talk",
    "call_notes": "notes on a call",
    "scheduled": "scheduled work: routines, the briefing, reminders",
    "tools": "the model's tools",
    "modules": "modules, the extensions Steve builds",
    "mcp": "MCP servers",
}


@dataclass(frozen=True)
class Cannot:
    """Why a door cannot reach a guarantee, in a sentence that names the
    code that makes it so."""

    why: str


@dataclass(frozen=True)
class Open:
    """A door that reaches a guarantee with nothing yet to hold it there:
    the product's choice to make, written down where the table is read
    and in docs/plan/handoff.md, never left as a blank."""

    why: str


ALL = tuple(GUARANTEES)

# The worker's routes and the supervisor's, by "app METHODS path" as
# tests/gate/test_doors.py writes them, with the guarantees each touches:
# - a route that decides a card touches the card, high-risk and sign-in;
# - one that reads the user's mail, days, notes or memory touches sign-in,
#   since the app, not the worker, hides them while signed out (only the
#   app holds the token: core/localapi.py);
# - one that starts a turn touches the cap, the card and secrets;
# - one that changes a knob, a switch, a passphrase or an account touches
#   the controls, and the secrets when it keeps one.
# An empty tuple is a decision too: the route says nothing private and
# changes nothing a guarantee protects.
ROUTES = {
    "supervisor GET /_sup/health": (),
    "supervisor GET /_sup/status": (),
    "supervisor POST /_sup/swap": ("controls",),
    "supervisor POST /_sup/stop": ("controls",),
    # Answers without the token, and proves the backend without giving it.
    "supervisor POST /_sup/prove": ("secrets",),
    # The two below carry the worker's routes, each placed below.
    "supervisor WS /ws": ALL,
    "supervisor DELETE,GET,OPTIONS,PATCH,POST,PUT /{path:path}": ALL,
    "worker GET /health": (),
    "worker POST /_shutdown": ("controls",),
    "worker POST /_activate": (),
    "worker POST /_retire": (),
    "worker POST /chat": ("card", "cap", "secrets", "signed_out"),
    "worker GET /status": (),
    "worker GET /voice/status": ("microphone",),
    "worker POST /voice/listen": ("microphone", "controls"),
    "worker POST /voice/tap": ("microphone", "controls"),
    "worker GET /attention/status": (),
    "worker GET /reminders": ("signed_out",),
    "worker POST /actions/decide": ("card", "high_risk", "signed_out"),
    "worker GET /approvals/setup": ("controls",),
    "worker POST /approvals/passphrase": ("controls", "high_risk", "secrets"),
    "worker POST /approvals/passphrase/reset": ("controls", "high_risk"),
    "worker POST /approvals/passphrase/reset/windows": ("controls", "high_risk", "secrets"),
    # Making a new approver takes the app's sign-in (agent/worker/signin.refused).
    "worker POST /approvals/authenticator": ("controls", "high_risk", "secrets", "signed_out"),
    "worker POST /approvals/authenticator/confirm": ("controls", "high_risk", "signed_out"),
    "worker POST /approvals/hello": ("controls", "high_risk"),
    "worker POST /approvals/hello/forget": ("controls", "high_risk"),
    "worker GET /signin/state": ("signed_out",),
    "worker POST /signin/passphrase": ("signed_out", "secrets"),
    "worker POST /signin/challenge": ("signed_out",),
    "worker POST /signin/hello": ("signed_out",),
    "worker POST /signin/alive": ("signed_out",),
    "worker POST /signin/end": ("signed_out",),
    "worker GET /signal/status": (),
    "worker POST /signal/install": ("controls",),
    "worker POST /signal/link": ("controls", "secrets", "signed_out"),
    "worker POST /signal/unlink": ("controls",),
    "worker GET /telegram/status": (),
    "worker POST /telegram/token": ("controls", "secrets", "signed_out"),
    "worker POST /telegram/pair": ("controls", "signed_out"),
    "worker POST /telegram/unpair": ("controls",),
    "worker GET /jobs": ("signed_out",),
    "worker WS /ws": ("card", "microphone", "cap", "secrets", "signed_out"),
    "worker POST /browser/live/open": ("secrets", "signed_out"),
    "worker GET /browser/live/frame": ("secrets", "signed_out"),
    "worker POST /browser/live/act": ("signed_out",),
    "worker POST /browser/live/save": ("secrets", "controls"),
    "worker POST /browser/live/stop": (),
    # A file the agent may upload, with a card each time.
    "worker POST /browser/live/outbox": ("card",),
    "worker GET /browser/logins": ("secrets",),
    "worker POST /browser/logins/forget": ("secrets", "controls"),
    "worker POST /browser/logins/import": ("secrets", "controls"),
    # Whether the agent asks before using a login.
    "worker POST /browser/logins/tier": ("card", "controls"),
    # The app's choices, Talk among them.
    "worker GET,POST /control/app": ("controls", "microphone"),
    "worker GET /control/overview": ("signed_out",),
    "worker GET /control/autonomy": ("signed_out",),
    "worker GET /control/activity": ("signed_out",),
    "worker GET /control/spend": ("cap",),
    "worker GET /control/models": (),
    "worker POST /control/routine": ("controls",),
    # A goal resumed with more budget.
    "worker POST /control/goal": ("controls", "cap"),
    "worker POST /control/note": (),
    "worker POST /control/stop": ("controls",),
    "worker POST /control/knobs": ("controls", "cap"),
    "worker POST /control/models/revert": ("controls", "cap"),
    "worker GET,PUT /control/profile": ("signed_out",),
    "worker GET /control/modules": (),
    "worker POST /control/modules": ("controls", "secrets"),
    "worker POST /control/accounts": ("controls", "secrets"),
    "worker GET /control/mailbox": ("signed_out",),
    "worker POST /control/mailbox": ("controls",),
    "worker GET /setup/state": ("controls",),
    "worker POST /setup/answer": ("controls",),
    "worker POST /setup/key": ("controls", "secrets", "cap"),
    # Which keys the Credential Manager holds, never their values.
    "worker GET /setup/keys": ("controls", "secrets"),
    "worker POST /setup/preset": ("controls",),
    "worker GET /setup/drive": (),
    "worker GET /setup/docker": (),
    "worker GET /control/day": ("signed_out",),
    "worker GET /control/days": ("signed_out",),
    "worker POST /control/looked": (),
    "worker POST /control/react": (),
    "worker GET /control/next": ("signed_out",),
    "worker GET /control/search": ("signed_out",),
    "worker POST /control/photo": (),
    "worker GET /control/photo": ("signed_out",),
    "worker GET /control/places": ("signed_out",),
    "worker GET /control/coming": ("signed_out",),
    "worker GET /control/calls": ("signed_out",),
    # Steve's questions and yeses, as notes: the cards among them.
    "worker GET /control/needs": ("card", "signed_out"),
    "worker POST /control/needs/done": (),
    "worker POST /control/needs/later": (),
    "worker POST /control/needs/not-now": (),
    "worker GET /control/knows": ("signed_out",),
    "worker POST /control/knows/correct": (),
    "worker POST /control/knows/forget": (),
    "worker GET /control/readers": (),
    "worker POST /control/readers/install": ("controls",),
}

UI = "tests/unit/ui/"
NOTES, BAR, CARD = f"{UI}test_notes_screen.py", f"{UI}test_quickbar_screen.py", \
    f"{UI}test_deskcard_screen.py"
SIGNIN, SETTINGS = f"{UI}test_signin_screen.py", f"{UI}test_app_settings_screen.py"
SHELL = "ui/src-tauri/src/lib.rs"
APPROVE = "tests/gate/test_who_may_approve.py"
DESK, ROUTINES = "tests/gate/test_desk.py", "tests/gate/test_routines.py"
CALLS, TALK = "tests/unit/test_call_notes.py", "tests/unit/test_push_to_talk.py"
MCP, SPEND = "tests/unit/test_mcp_servers.py", "tests/gate/test_spend.py"
SHARED_EAR = "test_the_ear_push_to_talk_and_call_notes_share_hears_nothing_past_the_cap"
MODULE_CARD_SIGNED_OUT = ("test_a_module_server_or_build_card_needs_a_live_session_once_a_"
                          "passphrase_is_set")
TELEGRAM = "tests/unit/test_telegram.py"
# The whole of the app's sign-in for a tray item that answers nothing.
TRAY_MENU = (f"The tray's menu is Open Steve, Talk, Show the desk card, Take notes and "
             f"Quit ({SHELL}, the menu built in `run`): none answers a card.")
NO_BUTTONS = ("A toast has no buttons (ui/src-tauri/src/toast.rs): a click opens the app "
              "on its notes, behind the sign-in.")
# A high-risk tool is one consent.yaml names (core/consent.py `high_risk`),
# by its name; a module's actions and a server's calls are each one tool.
HIGH_BY_NAME = "core/consent.py `high_risk` goes by the tool's name"

TABLE: dict[str, dict[str, str | tuple[str, ...] | Cannot | Open]] = {
    "card": {
        "window": ("tests/e2e/test_mail.py::test_a_reply_waits_in_drafts_on_a_card_naming_its_"
                   "recipient_and_goes_only_on_yes",
                   f"{NOTES}::test_a_note_in_the_margin_shows_what_it_needs_its_word_and_its_pin"),
        "quick_bar": (f"{BAR}::test_a_yes_in_the_bar_is_asked_with_the_card_whole_everyone_it_"
                      "reaches_in_view",
                      f"{BAR}::test_a_grouped_card_is_opened_in_steve_not_approved_whole_from_"
                      "the_bar"),
        "desk_card": Cannot("The desk card answers no card: it reads the notes and what is next "
                            "(ui/src/DeskCard.tsx), and its notes line opens the quick bar, "
                            "where a card is asked whole."),
        "tray": Cannot(TRAY_MENU),
        "toast": Cannot(NO_BUTTONS),
        "signal": ("tests/e2e/test_signal.py::test_a_card_arrives_as_a_note_to_self_and_yes_"
                   "with_its_word_runs_it",
                   "tests/e2e/test_signal.py::test_a_yes_with_the_wrong_word_runs_nothing"),
        "telegram": ("tests/e2e/test_telegram.py::test_a_cards_no_button_runs_nothing",
                     f"{TELEGRAM}::test_a_press_is_its_words_from_the_paired_chat_alone"),
        # A spoken yes is answered as the voice channel's (agent/worker/links.py).
        "push_to_talk": f"{APPROVE}::test_an_everyday_card_takes_a_plain_yes_recorded_as_its_"
                        "channel",
        "call_notes": f"{DESK}::test_what_the_laptop_plays_is_a_note_never_a_request",
        "scheduled": f"{ROUTINES}::test_a_run_sends_only_to_the_targets_the_user_approved",
        "tools": ("tests/gate/test_every_tool_is_classified.py::test_every_gatable_tool_"
                  "actually_routes_through_the_gate",
                  "tests/gate/test_approval_binding.py::test_what_runs_is_what_the_user_was_"
                  "asked_about_not_what_the_reply_says",
                  "tests/e2e/test_red_team.py::test_a_mail_that_asks_for_the_users_mail_gets_"
                  "only_cards_and_refusals",
                  "tests/e2e/test_red_team.py::test_a_web_page_that_asks_for_the_users_mail_"
                  "gets_only_cards_and_refusals",
                  "tests/e2e/test_red_team.py::test_a_calendar_invitation_that_asks_for_the_"
                  "users_mail_gets_only_cards_and_refusals",
                  "tests/e2e/test_red_team.py::test_a_forward_to_the_agents_mailbox_runs_"
                  "unwatched_and_reaches_nothing_unshown"),
        "modules": ("tests/gate/test_every_tool_is_classified.py::test_every_module_tool_is_"
                    "classified_by_its_manifest_and_asks_where_its_grants_say",
                    "tests/gate/test_module_tools.py::test_a_tools_call_cannot_post_where_its_"
                    "manifest_may_and_its_perform_can"),
        "mcp": (f"{MCP}::test_a_tool_the_server_labels_a_write_is_a_card_on_every_call_and_"
                "starts_nothing",
                f"{MCP}::test_with_no_one_watching_every_call_is_a_card_even_a_read_allowed_"
                "or_freed"),
    },
    "high_risk": {
        "window": (f"{NOTES}::test_a_card_that_spends_money_takes_the_passphrase_and_says_when_"
                   "it_is_wrong",
                   f"{SIGNIN}::test_a_high_risk_yes_carries_a_hello_signature_over_that_card",
                   "tests/e2e/test_high_risk.py::test_a_spending_card_refuses_a_yes_without_"
                   "the_passphrase_and_takes_one_with_it"),
        "quick_bar": f"{BAR}::test_a_high_risk_card_is_opened_in_steve_where_its_passphrase_is_"
                     "asked",
        "desk_card": Cannot("The desk card has no Yes (ui/src/DeskCard.tsx), and Windows Hello "
                            "signs for the main window alone (ui/src-tauri/src/signin.rs "
                            "`hello_sign`)."),
        "tray": Cannot(TRAY_MENU),
        "toast": Cannot(NO_BUTTONS),
        "signal": "tests/e2e/test_signal.py::test_a_high_risk_card_on_the_phone_takes_a_yes_"
                  "only_with_the_authenticators_code",
        "telegram": (f"{TELEGRAM}::test_a_spending_card_on_telegram_takes_the_code",
                     f"{APPROVE}::test_a_high_risk_yes_takes_the_code_on_each_channel"),
        "push_to_talk": f"{APPROVE}::test_a_high_risk_yes_takes_the_code_on_each_channel",
        "call_notes": Cannot("What a call says is written into its note and never becomes a "
                             "turn (agent/worker/desk.py `_segment`), so no yes comes of it."),
        "scheduled": f"{ROUTINES}::test_high_risk_tools_are_never_routinable",
        "tools": (f"{APPROVE}::test_a_second_factor_is_bound_to_the_arguments_it_was_given_for",
                  "tests/gate/test_browser_consent.py::test_a_page_that_takes_payment_gets_no_"
                  "card_at_all",
                  "tests/gate/test_browser_consent.py::test_a_logged_in_click_on_a_page_that_"
                  "takes_payment_gets_no_card_at_all",
                  "tests/gate/test_browser_logins.py::test_an_approved_click_on_a_page_that_"
                  "turned_into_a_checkout_is_refused"),
        "modules": Open("A module's action is one tool, module.perform (agent/modtools.py), "
                        f"and {HIGH_BY_NAME}: a perform that pays through a host the module "
                        "declares takes a plain yes. Only the module's own admission asks for "
                        "the passphrase or Hello."),
        "mcp": Open("Every call to a server is one tool, mcp.call (agent/mcptools.py), and "
                    f"{HIGH_BY_NAME}: a server's tool that pays takes a plain yes. Only an "
                    "\"anywhere\" server's admission asks for the passphrase (core/lifecycle.py)."),
    },
    "microphone": {
        "window": (f"{SETTINGS}::test_talk_turned_off_at_the_sidebars_foot_makes_a_hold_a_tap_"
                   "and_takes_the_microphone_away",
                   f"{SETTINGS}::test_a_tap_opens_the_quick_bar_and_a_hold_talks_in_it_until_"
                   "let_go"),
        "quick_bar": (f"{BAR}::test_a_held_hotkey_listens_in_the_bar_and_sends_what_was_heard_"
                      "when_let_go",
                      f"{BAR}::test_talk_from_a_menu_stops_listening_when_the_bar_is_clicked_"
                      "away"),
        "desk_card": (f"{CARD}::test_a_click_opens_today_the_notes_line_opens_the_quick_bar_and_"
                      "a_hold_talks",
                      f"{CARD}::test_with_talk_off_a_hold_of_the_card_is_taken_as_a_click_and_"
                      "hears_nothing"),
        # Talk from the tray opens the quick bar listening (finding 3).
        "tray": f"{BAR}::test_talk_from_a_menu_stops_listening_when_the_bar_is_clicked_away",
        "toast": Cannot(NO_BUTTONS),
        "signal": f"{CALLS}::test_notes_of_the_room_are_refused_from_anywhere_but_their_desk",
        "telegram": (f"{CALLS}::test_notes_of_the_room_are_refused_from_anywhere_but_their_desk",
                     f"{DESK}::test_the_microphone_is_opened_on_the_room_only_by_their_own_"
                     "request_at_the_desk"),
        "push_to_talk": (f"{TALK}::test_a_socket_gone_mid_press_closes_the_microphone_and_sends_"
                         "nothing",
                         f"{TALK}::test_a_second_press_after_a_cold_load_leaves_no_microphone_"
                         "open"),
        "call_notes": (f"{CALLS}::test_notes_of_the_room_need_the_microphone_on",
                       f"{DESK}::test_the_microphone_is_opened_on_the_room_only_by_their_own_"
                       "request_at_the_desk"),
        "scheduled": f"{DESK}::test_the_microphone_is_opened_on_the_room_only_by_their_own_"
                     "request_at_the_desk",
        "tools": (f"{DESK}::test_the_microphone_is_opened_on_the_room_only_by_their_own_request_"
                  "at_the_desk",
                  "tests/unit/test_hands_free_withheld.py::test_the_model_is_not_sent_to_a_"
                  "switch_the_app_does_not_have"),
        # A module's container has no microphone; COM is opened outside it.
        "modules": ("tests/gate/test_module_com.py::test_admission_refuses_a_manifest_that_"
                    "lists_a_sending_member_as_a_read_or_a_denied_program",
                    "tests/gate/test_module_com.py::test_a_program_or_member_the_manifest_does_"
                    "not_list_is_refused_from_the_sandbox"),
        "mcp": Cannot("A server Steve runs is a container with no microphone and no COM "
                      "(core/sandbox/servers.py, core/sandbox/runtime.py); one the user runs is "
                      "reached over HTTP alone (core/engines/mcp.py)."),
    },
    "cap": {
        "window": "tests/e2e/test_spend.py::test_at_the_months_cap_the_reply_says_so_a_routine_"
                  "waits_and_a_note_offers_more",
        # The bar asks over the window's socket; at the cap its note opens Steve.
        "quick_bar": f"{BAR}::test_a_fix_and_the_notes_that_dont_fit_open_steve_where_they_are_"
                     "done",
        "desk_card": Cannot("The desk card asks no model: it reads the notes, what is next and "
                            "the day (ui/src/DeskCard.tsx), and a hold talks through the quick "
                            "bar."),
        "tray": Cannot(f"The tray's items ask no model themselves ({SHELL}): Talk goes through "
                       "the quick bar and Take notes through notes on a call, each its own row."),
        "toast": Cannot(NO_BUTTONS),
        "signal": ("tests/unit/test_spend_caps.py::test_a_conversation_past_its_cap_is_answered_"
                   "with_no_model_call",
                   "tests/unit/test_server_signal.py::test_a_voice_note_is_billed_as_the_signal_"
                   "conversations_chat"),
        "telegram": "tests/unit/test_spend_caps.py::test_a_conversation_past_its_cap_is_answered_"
                    "with_no_model_call",
        "push_to_talk": (f"{TALK}::test_a_turn_outside_any_unit_of_work_is_billed_as_chat",
                         f"{TALK}::{SHARED_EAR}"),
        # Notes on a call hear through the same ear (agent/worker/desk.py).
        "call_notes": (f"{TALK}::{SHARED_EAR}",
                       f"{SPEND}::test_every_model_client_steve_builds_is_guarded"),
        "scheduled": "tests/e2e/test_spend.py::test_at_the_months_cap_the_reply_says_so_a_"
                     "routine_waits_and_a_note_offers_more",
        "tools": ("tests/gate/test_websearch.py::test_a_search_past_a_cap_is_refused_before_it_"
                  "is_made",
                  f"{SPEND}::test_every_model_client_steve_builds_is_guarded"),
        "modules": (f"{SPEND}::test_an_allowance_lifts_its_own_work_past_its_category_cap_and_"
                    "never_the_month_s",
                    "tests/unit/test_modbuild.py::test_a_build_the_cap_stops_keeps_its_draft_and_"
                    "goes_on_from_it_when_allowed"),
        "mcp": "tests/unit/test_mcp_stdio.py::test_a_servers_own_request_is_refused_so_it_cannot_"
               "ask_steves_model_anything",
    },
    "secrets": {
        "window": ("tests/e2e/test_setup.py::test_a_fresh_install_is_set_up_page_by_page_and_"
                   "opens_on_the_day_after_a_restart",
                   "tests/gate/test_no_secret_on_disk.py::test_after_setup_the_data_folder_"
                   "holds_no_password_token_or_client_secret"),
        # The token and the session are handed to the app's own pages alone.
        "quick_bar": f"{SHELL}::the_app_s_windows_stay_on_the_app_s_own_pages",
        "desk_card": f"{SHELL}::the_app_s_windows_stay_on_the_app_s_own_pages",
        "tray": Cannot(f"The menu's words are fixed, and its tooltip is a count and whether "
                       f"notes are on ({SHELL} `redraw_tooltip`)."),
        "toast": Cannot(f"A toast is handed a message's kind and words ({SHELL} `notify`), never "
                        "the token, the session or a key; the words are the chat's, which the "
                        "end-to-end canary scan reads (tests/e2e/conftest.py)."),
        # Every end-to-end scenario ends with the canary scan of what each
        # fake was sent (tests/e2e/conftest.py `_canaries`).
        "signal": ("tests/unit/test_signal_link.py::test_the_link_is_read_while_signal_cli_waits_"
                   "and_written_nowhere",
                   "tests/e2e/test_signal.py::test_a_card_arrives_as_a_note_to_self_and_yes_"
                   "with_its_word_runs_it"),
        "telegram": (f"{TELEGRAM}::test_the_token_never_reaches_an_error_or_the_status",
                     "tests/e2e/test_telegram.py::test_a_cards_yes_button_runs_it"),
        # What fails is said by its status alone, never OpenAI's words.
        "push_to_talk": "tests/gate/test_hearing.py::test_with_no_answer_it_says_why_and_bills_"
                        "nothing",
        "call_notes": Cannot("Notes on a call keep the call's words alone: a stretch's failure "
                             "is dropped (agent/worker/desk.py `_segment`), and the key is read "
                             "and used inside core/hearing.py alone."),
        "scheduled": "tests/e2e/test_briefing.py::test_the_morning_briefing_reaches_the_phone_"
                     "and_the_day_once",
        "tools": ("tests/gate/test_browser_logins.py::test_the_driver_types_their_login_on_its_"
                  "own_site_and_says_nothing_of_it",
                  "tests/gate/test_browser_logins.py::test_no_tool_a_model_calls_can_type_click_"
                  "as_them_or_take_a_session"),
        "modules": ("tests/gate/test_module_context.py::test_another_modules_key_reaches_none_"
                    "of_this_modules_secrets",
                    "tests/gate/test_module_broker.py::test_a_server_that_echoes_a_filled_"
                    "secret_does_not_hand_it_to_the_module"),
        "mcp": (f"{MCP}::test_the_secret_is_kept_under_the_install_and_sent_only_in_its_bound_"
                "header",
                "tests/gate/test_no_secret_on_disk.py::test_a_servers_home_is_the_one_place_a_"
                "secret_may_be_and_only_inside_it"),
    },
    "controls": {
        "window": (f"{SIGNIN}::test_with_a_passphrase_set_the_app_shows_nothing_until_it_is_typed",
                   f"{SIGNIN}::test_a_forgotten_passphrase_is_reset_in_settings_with_a_glance"),
        "quick_bar": f"{BAR}::test_a_fix_and_the_notes_that_dont_fit_open_steve_where_they_are_"
                     "done",
        "desk_card": Cannot("Through its own face the desk card writes only where it sits and "
                            "when it hides (ui/src-tauri/src/deskcard.rs); its settings open the "
                            "main window, behind the sign-in."),
        "tray": (f"{SIGNIN}::test_signed_out_the_trays_take_notes_brings_the_sign_in_and_starts_"
                 "nothing",
                 f"{SIGNIN}::test_signed_out_the_trays_show_the_desk_card_leaves_the_users_"
                 "setting_alone"),
        "toast": Cannot(NO_BUTTONS),
        # The phone's own words (unlink, the code) are read by code, from
        # their own chat alone (agent/worker/answering.py).
        "signal": f"{APPROVE}::test_no_tool_reaches_what_decides_who_may_approve",
        "telegram": (f"{TELEGRAM}::test_only_the_apps_code_sent_in_time_pairs_a_chat",
                     f"{APPROVE}::test_no_tool_reaches_what_decides_who_may_approve"),
        "push_to_talk": ("tests/unit/test_hands_free_withheld.py::test_the_worker_itself_refuses_"
                         "to_switch_it_on",
                         f"{APPROVE}::test_no_tool_reaches_what_decides_who_may_approve"),
        "call_notes": f"{DESK}::test_a_lecture_is_summarised_by_a_model_with_no_tools",
        "scheduled": (f"{ROUTINES}::test_only_their_own_conversations_can_make_or_restart_a_"
                      "routine",
                      f"{ROUTINES}::test_a_worker_or_a_routine_cannot_make_one_even_on_their_"
                      "channel"),
        "tools": (f"{APPROVE}::test_no_tool_reaches_what_decides_who_may_approve",
                  "tests/gate/test_only_the_app.py::test_only_the_apps_backend_names_an_account_"
                  "or_secret_writer"),
        "modules": ("tests/gate/test_module_sandbox.py::test_a_module_reaches_neither_the_"
                    "internet_nor_this_computers_own_ports",
                    "tests/gate/test_module_imports.py::test_a_module_imports_only_the_stdlib_"
                    "the_sdk_its_own_files_and_its_declared_libraries",
                    "tests/unit/test_modbuild.py::test_while_the_switch_is_off_only_the_user_may_"
                    "ask_for_a_build"),
        "mcp": ("tests/gate/test_local_servers.py::test_a_server_reaches_its_approved_host_and_"
                "nothing_else",
                "tests/gate/test_local_servers.py::test_a_server_writes_its_home_and_reads_"
                "nothing_else_of_the_users"),
    },
    "signed_out": {
        "window": (f"{SIGNIN}::test_with_a_passphrase_set_the_app_shows_nothing_until_it_is_typed",
                   f"{SIGNIN}::test_a_yes_the_backend_refuses_for_want_of_a_session_brings_back_"
                   "the_sign_in",
                   "tests/unit/test_signin.py::test_an_answer_from_the_app_needs_a_live_session_"
                   "once_a_passphrase_is_set"),
        "quick_bar": f"{SIGNIN}::test_signed_out_the_quick_bar_shows_nothing_and_hears_nothing",
        "desk_card": f"{SIGNIN}::test_signed_out_the_desk_card_is_the_mark_alone_and_opens_the_"
                     "app",
        # Its dot and tooltip still count the notes waiting, as a signed-out
        # toast still says that something came.
        "tray": (f"{SIGNIN}::test_signed_out_the_trays_take_notes_brings_the_sign_in_and_starts_"
                 "nothing",
                 f"{SIGNIN}::test_signed_out_the_trays_show_the_desk_card_leaves_the_users_"
                 "setting_alone"),
        "toast": (f"{SIGNIN}::test_signed_out_a_toast_says_only_that_something_came",
                  "ui/src-tauri/src/toast.rs::a_locked_screen_shows_only_the_kind"),
        "signal": Cannot("The sign-in is the app's: the phone the user paired is them, signed in "
                         "or not, and its yes is checked by its word and code "
                         "(agent/worker/answering.py, docs/security-model.md)."),
        "telegram": Cannot("The sign-in is the app's: the chat the user paired is them, signed "
                           "in or not, and its yes is checked by its button and code "
                           "(agent/worker/answering.py, docs/security-model.md)."),
        "push_to_talk": f"{SIGNIN}::test_signed_out_the_quick_bar_shows_nothing_and_hears_nothing",
        "call_notes": f"{SIGNIN}::test_signed_out_the_trays_take_notes_brings_the_sign_in_and_"
                      "starts_nothing",
        "scheduled": (f"{SIGNIN}::test_signed_out_a_toast_says_only_that_something_came",
                      "tests/gate/test_what_review_protected.py::test_only_their_own_channels_"
                      "may_answer_a_card"),
        "tools": "tests/gate/test_what_review_protected.py::test_nothing_but_their_reply_and_the_"
                 "tray_can_decide_a_card",
        "modules": f"tests/unit/test_lifecycle.py::{MODULE_CARD_SIGNED_OUT}",
        "mcp": f"tests/unit/test_lifecycle.py::{MODULE_CARD_SIGNED_OUT}",
    },
}
