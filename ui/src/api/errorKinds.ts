/** Plain words for the failure classes a request row carries.
 *
 *  A usage bucket's `errors` is keyed by `error_class || error_code`
 *  (`control_plane/telemetry/retention.py`): either a refusal code the gateway
 *  wrote (`no_target`, `limit_exceeded`, …) or the name of the exception that
 *  ended an attempt (`ReadTimeout`, `ClientDisconnected`, …). The set is open,
 *  so an unknown name is shown as itself rather than guessed at. */
const KINDS: Record<string, string> = {
  // Refusals: the gateway answered without reaching a model.
  no_target: 'nothing could serve the model',
  model_not_found: 'unknown model',
  model_not_ready: 'model still starting',
  wrong_modality: 'wrong endpoint for the model',
  model_not_allowed: 'model not allowed for the key',
  limit_exceeded: 'key or org limit reached',
  max_output_tokens_exceeded: 'asked for too many output tokens',
  max_prompt_tokens_exceeded: 'prompt over the key’s limit',
  invalid_request: 'malformed request',
  context_length_exceeded: 'longer than the model’s context',
  kv_cache_exhausted: 'model out of KV cache',
  auth_unavailable: 'key store unavailable',
  missing_api_key: 'no API key',
  invalid_api_key: 'bad API key',
  api_key_disabled: 'API key disabled',
  api_key_expired: 'API key expired',
  provider_key_unavailable: 'provider API key missing',
  provider_adapter_unsupported: 'provider cannot take this request',
  realtime_refused: 'realtime session refused',
  // Attempts that failed on the wire (httpx exception names and the proxy's own).
  ConnectError: 'could not reach the model server',
  ConnectTimeout: 'model server did not accept in time',
  ReadTimeout: 'model server stopped answering',
  WriteTimeout: 'model server stopped reading',
  PoolTimeout: 'no free upstream connection',
  RemoteProtocolError: 'model server broke the connection',
  ReadError: 'connection lost mid-answer',
  WriteError: 'connection lost sending the request',
  ClientDisconnected: 'client hung up',
  Discarded: 'duplicate attempt dropped',
  RateLimitError: 'provider rate limit',
  UpstreamError: 'provider returned an error',
}

/** The class an error-status answer has when nothing named it. */
export const UNCLASSIFIED = 'unclassified'

/** "model server stopped answering (ReadTimeout)"; an unknown class as itself. */
export function errorKind(cls: string): string {
  if (cls === '' || cls === UNCLASSIFIED) return `upstream error status (${UNCLASSIFIED})`
  const words = KINDS[cls]
  return words ? `${words} (${cls})` : cls
}
