import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { AdminIdentityProvider } from './auth/AdminIdentity.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AdminIdentityProvider>
      <App />
    </AdminIdentityProvider>
  </StrictMode>,
)
