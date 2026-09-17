import { closePool } from './helpers/db';
import { stopStreamlit } from './helpers/streamlitProcess';

export default async function globalTeardown(): Promise<void> {
  stopStreamlit();
  await closePool();
  // Postgres container is deliberately left running - cheap, and other
  // Python tests/manual use may still want it available.
}
