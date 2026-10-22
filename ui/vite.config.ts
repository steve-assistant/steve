import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The backend's paths. In the "preview" mode alone (`python -m
// devtools.preview`) they are passed on to a backend on the test fakes, with
// its token added here, so the page never holds one; the desktop app's dev
// and build modes proxy nothing.
const BACKEND_PATHS = ['/control', '/_sup', '/actions', '/approvals', '/browser', '/inbox',
  '/jobs', '/night', '/signal', '/status', '/telegram', '/voice']

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const backend = process.env.STEVE_UI_PREVIEW_BACKEND
  const token = process.env.STEVE_UI_PREVIEW_TOKEN
  const proxy = mode === 'preview' && backend && token
    ? Object.fromEntries(BACKEND_PATHS.map((path) => [path, {
        target: backend, headers: { Authorization: `Bearer ${token}` } }]))
    : undefined
  // The "specimen" mode adds the mark's specimen sheet (specimen.html) for
  // the screen tests; the app's own build has only its window.
  const input = mode === 'specimen'
    ? { main: 'index.html', specimen: 'specimen.html' }
    : undefined
  // Setup shows the privacy statement, PRIVACY.md, from the repository's
  // root. The dev server serves files from outside ui/ only if named here:
  // the file alone, as the root holds data/ and the backend's token.
  const fs = { allow: ['.', '../PRIVACY.md'] }
  return { plugins: [react()], server: { proxy, fs }, build: { rollupOptions: { input } } }
})
