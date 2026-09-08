import { useRef } from 'react'

/** What `POST /v1/audio/transcriptions` is given besides the model.
 *
 *  A sibling of `VoiceFields` rather than a mode inside it: the two endpoints
 *  take disjoint inputs -- one takes a voice and a container to write, the
 *  other takes a file and the language inside it -- and a component that drew
 *  both would be four controls of which two are always inert. */
export interface Upload {
  file: File | null
  language: string
}

/** The default, and a visible control rather than a silent parameter.
 *
 *  `whisper-*.en` has no language tokens, so when the field is absent vLLM
 *  runs auto-detection and the model fails an assertion inside
 *  `supported_languages` -- a 500, from a request that looks fine. Sending
 *  `en` invisibly would fix the English case and make a multilingual
 *  checkpoint mysteriously monolingual; a field defaulting to `en` fixes both
 *  and costs one control. */
export const DEFAULT_LANGUAGE = 'en'

/** What the browser will let somebody choose. Not a claim about what the
 *  runtime decodes -- that is the upstream's business and its refusal is
 *  rendered verbatim if it disagrees -- only a filter that stops the picker
 *  offering a text file. */
const ACCEPT = 'audio/*,.wav,.mp3,.m4a,.flac,.ogg,.opus,.webm'

function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const kib = bytes / 1024
  if (kib < 1024) return `${kib.toFixed(1)} KiB`
  return `${(kib / 1024).toFixed(2)} MiB`
}

interface Props {
  disabled: boolean
  value: Upload
  onChange: (next: Upload) => void
  idPrefix: string
}

export function UploadFields({ disabled, value, onChange, idPrefix }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)

  return (
    <div className="speechrow">
      <div className="fld">
        <label htmlFor={`${idPrefix}-file`}>Audio</label>
        {/* The native control, styled by nothing: a custom button plus a
            hidden input is the usual trick and it costs the file name, the
            drag target and the keyboard behaviour the browser gives free. */}
        <input
          ref={inputRef}
          id={`${idPrefix}-file`}
          type="file"
          accept={ACCEPT}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, file: e.target.files?.[0] ?? null })}
        />
      </div>

      <div className="fld">
        <label htmlFor={`${idPrefix}-language`}>Language</label>
        <input
          id={`${idPrefix}-language`}
          type="text"
          size={4}
          spellCheck={false}
          placeholder="en"
          disabled={disabled}
          value={value.language}
          onChange={(e) => onChange({ ...value, language: e.target.value.trim() })}
        />
      </div>

      <span style={{ flex: 1 }} />

      {value.file ? (
        <span className="unit">{size(value.file.size)}</span>
      ) : (
        <span className="unit">no file chosen</span>
      )}
    </div>
  )
}
