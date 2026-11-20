// The chat answer's markdown renderer. Two things types cannot see: that the
// block parser files each line where a reader expects it (a streaming fence is
// code, a list item's continuation stays in the item), and that model output
// can never become live markup -- an `<img onerror>` or a `javascript:` link
// in an answer is text, and only an http(s) link is a link. Rendered with
// react-dom/server against the real component, so this is the markup the
// browser gets.
import { build } from 'esbuild'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { report } from '../../check/harness.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..', '..')

const R = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
// Under node_modules so the bundle's bare `react` resolves to the same copy
// imported above -- two copies do not share a dispatcher.
const cache = join(uiRoot, 'node_modules', '.cache', 'markdown-check')
mkdirSync(cache, { recursive: true })
const outfile = join(cache, 'Markdown.mjs')
await build({
  entryPoints: [join(here, 'Markdown.tsx')],
  bundle: true,
  format: 'esm',
  outfile,
  jsx: 'automatic',
  external: ['react', 'react-dom', 'react/jsx-runtime'],
  logLevel: 'warning',
})
const M = await import(pathToFileURL(outfile).href)
const { check, done } = report()

const html = (text) => renderToStaticMarkup(R.createElement(M.Markdown, { text }))

{
  const blocks = M.parseMarkdown([
    '## Steps',
    '',
    'First line',
    'second line',
    '- one',
    '  continued',
    '- two',
    '3. three',
    '4) four',
    '> quoted',
    '> more',
    '---',
    '```py',
    'x = 1',
    '```',
    'after',
  ].join('\n'))
  const kinds = blocks.map((b) => b.kind).join(',')
  check(kinds === 'heading,p,ul,ol,quote,hr,fence,p', `block shapes in order (${kinds})`)
  check(blocks[1].text === 'First line\nsecond line', 'consecutive lines are one paragraph')
  check(JSON.stringify(blocks[2].items) === JSON.stringify(['one\ncontinued', 'two']), 'an indented line continues the list item')
  check(blocks[3].start === 3 && blocks[3].items.length === 2, 'ordered list keeps its start and takes both delimiters')
  check(blocks[4].text === 'quoted\nmore', 'quote lines join')
  check(blocks[6].lang === 'py' && blocks[6].text === 'x = 1', 'fence keeps its language and body')
}

{
  const out = html('Here:\n```js\nconst a = 1\nmore')
  check((out.match(/<pre/g) ?? []).length === 1 && out.includes('const a = 1\nmore'), 'an unclosed fence (still streaming) renders as one <pre> to the end')
}
{
  const out = html('<img src=x onerror=alert(1)>')
  check(!out.includes('<img') && out.includes('&lt;img src=x onerror=alert(1)&gt;'), 'raw HTML is escaped text')
}
{
  const out = html('[x](javascript:alert(1))')
  check(!out.includes('<a'), 'a javascript: link is not a link')
}
{
  const out = html('see [x](https://a.b)')
  check(out.includes('<a href="https://a.b" target="_blank" rel="noopener noreferrer">x</a>'), 'an https link opens in a new tab without an opener')
}
{
  const out = html('use `a*b*` here')
  check(out.includes('<code>a*b*</code>') && !out.includes('<em>'), 'asterisks inside code are kept, not emphasis')
}
{
  const out = html('**bold** and *em* and _em2_ and snake_case_name and 2*3*4')
  check(out.includes('<strong>bold</strong>') && out.includes('<em>em</em>') && out.includes('<em>em2</em>'), 'strong and both emphasis spellings')
  check(out.includes('snake_case_name') && out.includes('2*3*4'), 'intraword underscores and asterisks stay literal')
}
{
  const out = html('![alt](https://a.b/i.png)')
  check(!out.includes('<img') && !out.includes('<a') && out.includes('![alt](https://a.b/i.png)'), 'an image is literal text: not loaded, not a link')
}
{
  const out = html('# H1\n###### H6')
  check((out.match(/<p class="label">/g) ?? []).length === 2 && !out.includes('<h1'), 'every heading level renders as .label')
}

done()
