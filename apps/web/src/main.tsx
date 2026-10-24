import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { AppProviders } from './app/providers';
import { AppRouter } from './app/router';
import './index.css';

const rootEl = document.getElementById('root');
if (!rootEl) {
  throw new Error('Root element #root not found');
}

createRoot(rootEl).render(
  <StrictMode>
    <AppRouter>
      <AppProviders>
        <App />
      </AppProviders>
    </AppRouter>
  </StrictMode>,
);
