import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Icon } from '../../components/Icon'
import { Lamp } from '../../components/Lamp'
import { Select } from '../../components/Select'
import type { SelectOption } from '../../components/Select'
import { SORTS } from './rows'
import type { ModelRow, Sort } from './rows'
import { filterMenu, parseHfLink, suggest } from './query'
import type { FilterMenuSection, SuggestNode, Suggestion } from './query'
import { OwnerMark, useAvatars } from './OwnerMark'
import { ownerOf } from './owner'
import { bandWord } from './verdictView'
import { LIST_VIEWS } from './useModelList'
import type { ListView } from './useModelList'

export interface ModelToolbarProps {
  query: string
  onQuery(q: string): void
  view: ListView
  onView(v: ListView): void
  counts: Record<ListView, number>
  sort: Sort
  onSort(s: Sort): void
  nodes: SuggestNode[]
  rows: ModelRow[]
  filterTokens: string[]
  shownCount: number
  showDownloads: boolean
  onShowDownloads(v: boolean): void
  downloadsBadge: number
  onOpen(modelId: string, quant: string | null, fromLink: boolean): void
  linkNote: string
  onDismissLinkNote(): void
  /** The server's pin list, for the `pinned` filter's suggestions. */
  pins?: ReadonlySet<string>
  /** Opens the add-a-provider dialog. */
  onAddProvider(): void
}

/** The query with one filter token taken out. The first exact occurrence
 *  only: two identical tokens are one filter, and the chip names one. */
function withoutToken(query: string, token: string): string {
  const parts = query.split(/\s+/).filter(Boolean)
  const at = parts.indexOf(token)
  if (at >= 0) parts.splice(at, 1)
  return parts.join(' ')
}

/** Appended, never replacing: a help button adds to what was typed. */
function appended(query: string, token: string): string {
  const q = query.trimEnd()
  return q ? `${q} ${token}` : token
}

/** The text with the matched range marked, so a suggestion shows WHY it
 *  matched rather than asking you to find the needle yourself. */
function Hit({ text, hit }: { text: string; hit: [number, number] | null }) {
  if (!hit) return <>{text}</>
  const [a, b] = hit
  return (
    <>
      {text.slice(0, a)}
      <mark>{text.slice(a, b)}</mark>
      {text.slice(b)}
    </>
  )
}

/** The filter dropdown: every category with the values this cluster has.
 *  Each choice toggles its token in the box -- the box stays the one place a
 *  filter lives, so the chips, the box and this menu cannot disagree. It
 *  stays open across picks, because filters combine. */
function FilterMenu({
  query,
  onQuery,
  active,
  sections,
}: {
  query: string
  onQuery(q: string): void
  active: string[]
  sections: FilterMenuSection[]
}) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const items = () => [
    ...(wrap.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemcheckbox"]') ?? []),
  ]

  useEffect(() => {
    if (!open) return
    items()[0]?.focus()
    const outside = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!open) return
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
      button.current?.focus()
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const list = items()
    const at = list.indexOf(document.activeElement as HTMLButtonElement)
    const next = e.key === 'ArrowDown' ? (at + 1) % list.length : at <= 0 ? list.length - 1 : at - 1
    list[next]?.focus()
  }

  return (
    <div ref={wrap} className="mdl-fmenu" onKeyDown={onKeyDown}>
      <button
        ref={button}
        type="button"
        className="mdl-search-btn mono"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        fits: on: quant:
        {active.length ? <span className="mdl-badge">{active.length}</span> : null}
        <Icon name="chevron-down" size={12} decorative />
      </button>
      {open ? (
        <div role="menu" aria-label="Filters" className="mdl-fmenu-panel">
          <div className="mdl-fmenu-groups">
            {sections.map((s) => (
              <div key={s.title} role="group" aria-label={s.title} className="mdl-fmenu-group">
                <div className="mdl-suggest-title">{s.title}</div>
                {s.choices.map((c) => {
                  const on = active.includes(c.token)
                  return (
                    <button
                      key={c.token}
                      type="button"
                      role="menuitemcheckbox"
                      aria-checked={on}
                      className="mdl-fmenu-item"
                      onClick={() => onQuery(on ? withoutToken(query, c.token) : appended(query, c.token))}
                    >
                      <span className="mdl-fmenu-check" aria-hidden="true">
                        {on ? <Icon name="check" size={12} decorative /> : null}
                      </span>
                      <span className="mono mdl-fmenu-token">{c.token}</span>
                      {c.note ? <span className="unit mdl-fmenu-note">{c.note}</span> : null}
                    </button>
                  )
                })}
              </div>
            ))}
          </div>
          <div className="ddfoot">Or type them in the box. Paste a huggingface.co link there to open any model.</div>
        </div>
      ) : null}
    </div>
  )
}

/** The search bar: view, box, sort, and the two doors out of the list.
 *
 *  The suggestion list is data from `query.ts::suggest`; this component only
 *  moves an active index across it and applies what was chosen. Choosing a
 *  filter rewrites the box and keeps focus, because a filter is half a
 *  question; choosing a model opens it, because that is a whole one. */
export function ModelToolbar({
  query,
  onQuery,
  view,
  onView,
  counts,
  sort,
  onSort,
  nodes,
  rows,
  filterTokens,
  shownCount,
  showDownloads,
  onShowDownloads,
  downloadsBadge,
  onOpen,
  linkNote,
  onDismissLinkNote,
  pins,
  onAddProvider,
}: ModelToolbarProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const blurTimer = useRef<number | null>(null)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)

  useAvatars()
  const sections = useMemo(() => suggest(query, { nodes, rows, pins }), [query, nodes, rows, pins])
  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections])
  const menu = useMemo(() => filterMenu({ nodes, rows }), [nodes, rows])
  const shown = open && sections.length > 0

  const apply = (item: Suggestion) => {
    if (item.action.type === 'query') {
      onQuery(item.action.query)
      setActive(-1)
      setOpen(true)
      inputRef.current?.focus()
      return
    }
    onOpen(item.action.modelId, item.action.quant, item.kind === 'link')
    setOpen(false)
    setActive(-1)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!flat.length) return
      e.preventDefault()
      setOpen(true)
      const step = e.key === 'ArrowDown' ? 1 : -1
      setActive((i) => (i < 0 && step < 0 ? flat.length - 1 : (i + step + flat.length) % flat.length))
      return
    }
    if (e.key === 'Escape') {
      if (open) {
        e.preventDefault()
        setOpen(false)
        setActive(-1)
      }
      return
    }
    if (e.key === 'Enter') {
      const item = shown && active >= 0 ? flat[active] : undefined
      if (item) {
        e.preventDefault()
        apply(item)
        return
      }
      // A pasted link is a whole request on its own: Enter opens it whether
      // or not the list was showing it.
      if (parseHfLink(query.trim())) {
        const link = flat.find((s) => s.kind === 'link')
        if (link) {
          e.preventDefault()
          apply(link)
        }
      }
      return
    }
    if (e.key === 'Tab' && shown) {
      const first = flat.find((s) => s.kind === 'filter')
      if (first) {
        e.preventDefault()
        apply(first)
      }
    }
  }

  const viewOptions = LIST_VIEWS.map(
    (v): SelectOption<ListView> => ({ value: v.id, label: `${v.label} · ${counts[v.id]}` }),
  )
  const sortOptions = SORTS.map((s): SelectOption<Sort> => ({ value: s.id, label: s.label }))

  let index = -1
  return (
    <div className="mdl-toolbar">
      <div className="mdl-search">
        <Select aria-label="View" value={view} options={viewOptions} onChange={onView} />

        <div className="mdl-search-box">
          <span className="mdl-search-icon">
            <Icon name="search" size={14} decorative />
          </span>
          <input
            ref={inputRef}
            className="mdl-search-input"
            role="combobox"
            aria-label="Search models"
            aria-controls="mdl-suggest"
            aria-expanded={shown}
            aria-autocomplete="list"
            aria-activedescendant={shown && active >= 0 ? `mdl-suggest-${active}` : undefined}
            spellCheck={false}
            // The browser's own remembered-entries list opened over the
            // suggestions and filled the box with an old query.
            autoComplete="off"
            value={query}
            placeholder={
              view === 'hub'
                ? 'Search Hugging Face, paste a link, or type task:vision'
                : 'Search, paste a huggingface.co link, or type  fits  on:<node>  quant:q4'
            }
            onChange={(e) => {
              onQuery(e.target.value)
              setOpen(true)
              setActive(-1)
            }}
            onFocus={() => {
              if (blurTimer.current != null) window.clearTimeout(blurTimer.current)
              setOpen(true)
            }}
            onBlur={() => {
              // Late, so a click on an item lands before the list goes.
              blurTimer.current = window.setTimeout(() => {
                setOpen(false)
                setActive(-1)
              }, 120)
            }}
            onKeyDown={onKeyDown}
          />
          {query ? (
            <button
              type="button"
              className="mdl-search-clear"
              aria-label="Clear search"
              onClick={() => {
                onQuery('')
                inputRef.current?.focus()
              }}
            >
              <Icon name="close" size={12} decorative />
            </button>
          ) : null}

          {shown ? (
            <div id="mdl-suggest" role="listbox" aria-label="Suggestions" className="mdl-suggest">
              {sections.map((s) => (
                <div key={s.title} role="group" aria-label={s.title} className="mdl-suggest-section">
                  <div className="mdl-suggest-title">{s.title}</div>
                  {s.items.map((item) => {
                    index += 1
                    const i = index
                    return (
                      <div
                        key={`${item.kind}:${item.text}`}
                        id={`mdl-suggest-${i}`}
                        role="option"
                        aria-selected={i === active}
                        className="mdl-suggest-item"
                        onMouseDown={(e) => {
                          e.preventDefault()
                          apply(item)
                        }}
                        onMouseEnter={() => setActive(i)}
                      >
                        {item.kind === 'model' ? (
                          <OwnerMark owner={ownerOf(item.text)} variant="inline" />
                        ) : null}
                        {item.band ? (
                          <Lamp
                            signal={bandWord(item.band, false).signal}
                            hollow={bandWord(item.band, false).signal === 'idle'}
                            label={bandWord(item.band, false).label}
                          />
                        ) : null}
                        <span className="mdl-suggest-text">
                          <Hit text={item.text} hit={item.hit} />
                        </span>
                        <span className="mdl-suggest-note">{item.note}</span>
                      </div>
                    )
                  })}
                </div>
              ))}
              <div className="mdl-suggest-foot" aria-hidden="true">
                ↑↓ move · Tab complete filter · Enter open · Esc close · Paste a huggingface.co
                link to open any model
              </div>
            </div>
          ) : null}
        </div>

        <FilterMenu query={query} onQuery={onQuery} active={filterTokens} sections={menu} />

        <Select aria-label="Sort models" value={sort} options={sortOptions} onChange={onSort} />

        <button
          type="button"
          className="mdl-search-btn"
          aria-pressed={showDownloads}
          onClick={() => onShowDownloads(!showDownloads)}
        >
          Downloads
          {downloadsBadge > 0 ? <span className="mdl-badge">{downloadsBadge}</span> : null}
        </button>

        <button
          type="button"
          className="mdl-search-btn"
          onClick={onAddProvider}
        >
          <Icon name="plus" size={14} decorative />
          Provider
        </button>
      </div>

      {filterTokens.length ? (
        <div className="mdl-filters">
          <span className="unit">Filtered by</span>
          {filterTokens.map((t) => (
            <span key={t} className="mdl-filter-chip mono">
              {t}
              <button
                type="button"
                className="mdl-filter-remove"
                aria-label={`Remove filter ${t}`}
                onClick={() => onQuery(withoutToken(query, t))}
              >
                <Icon name="close" size={12} decorative />
              </button>
            </span>
          ))}
          <span className="unit">{shownCount.toLocaleString()} shown</span>
        </div>
      ) : null}

      {linkNote ? (
        <div className="mdl-linknote" role="status">
          <Icon name="link" size={14} decorative />
          <span className="label">{linkNote}</span>
          <button
            type="button"
            className="mdl-filter-remove"
            aria-label="Dismiss"
            onClick={onDismissLinkNote}
          >
            <Icon name="close" size={12} decorative />
          </button>
        </div>
      ) : null}
    </div>
  )
}
