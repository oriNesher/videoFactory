import { StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

const container = document.getElementById('root')!

/**
 * One React root per container, ever.
 *
 * `createRoot` on a container that already has a root does not replace it: it
 * mounts a second, independent tree into the same element, and the whole app
 * appears twice with two separate copies of its state. Nothing in a normal page
 * load does that, but a dev session can — a module re-executed by HMR, or the
 * entry evaluated twice — and the result is a page that looks duplicated while
 * the source renders each panel exactly once. Caching the root on the container
 * makes a second evaluation re-render instead of re-mount.
 */
type RootHolder = HTMLElement & { _reactRoot?: Root }

const holder = container as RootHolder
const root = holder._reactRoot ?? createRoot(container)
holder._reactRoot = root

root.render(
  <StrictMode>
    <App />
  </StrictMode>,
)
