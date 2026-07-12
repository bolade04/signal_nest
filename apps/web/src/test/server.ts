import { setupServer } from 'msw/node';
import { handlers } from './handlers';

// Shared MSW server for the test suite. Individual tests may override specific
// handlers with server.use(...) to exercise error / empty states.
export const server = setupServer(...handlers);
