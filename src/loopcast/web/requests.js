// Request identity is distinct from permanent content/publication identity.
export function requestKey(source = globalThis.crypto) {
  if (typeof source?.randomUUID === 'function') return source.randomUUID();
  if (typeof source?.getRandomValues !== 'function') throw new Error('Secure randomness is unavailable. Use a supported browser and try again.');
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}
