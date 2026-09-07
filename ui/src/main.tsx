import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import './styles/tokens.css'
import './styles/base.css'
import './styles/derate.css'
import { AppShell } from './shell/AppShell'
import { BackendProvider } from './state/backend'
import { RouterProvider } from './state/router'
import { MetricsProvider } from './state/metrics'
import { TelemetryProvider } from './state/telemetry'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Outermost: the URL decides which screen mounts and what is selected on
        it, so everything below is downstream of the address bar. */}
    <RouterProvider>
      <BackendProvider>
        <MetricsProvider>
          <TelemetryProvider>
            <AppShell />
          </TelemetryProvider>
        </MetricsProvider>
      </BackendProvider>
    </RouterProvider>
  </StrictMode>,
)
