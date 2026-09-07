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
import { MetricsProvider } from './state/metrics'
import { TelemetryProvider } from './state/telemetry'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BackendProvider>
      <MetricsProvider>
        <TelemetryProvider>
          <AppShell />
        </TelemetryProvider>
      </MetricsProvider>
    </BackendProvider>
  </StrictMode>,
)
