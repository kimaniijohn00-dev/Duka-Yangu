// Checks every meaningful text/background and UI-boundary colour pair in
// both themes against WCAG AA, and fails the build if any pair is short.
// It also fails on hard-coded colours outside the html[data-theme] token
// blocks and on var() references with no token definition, so the theme
// cannot silently drift out of the single source of truth.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const cssPath = join(here, '..', 'src', 'styles.css')
const srcDir = join(here, '..', 'src')
const css = readFileSync(cssPath, 'utf8')

let failures = 0
function fail(message) {
  failures += 1
  console.error(`FAIL: ${message}`)
}

// ---------------------------------------------------------------------------
// 1. Parse the two theme token blocks: html[data-theme='light'|'dark'].
// ---------------------------------------------------------------------------
function themeBlock(theme) {
  const pattern = new RegExp(
    `html\\[data-theme=['"]${theme}['"]\\]\\s*\\{([\\s\\S]*?)\\n\\}`,
  )
  const match = css.match(pattern)
  if (!match) {
    fail(`missing html[data-theme='${theme}'] token block`)
    return ''
  }
  return match[1]
}

function parseTokens(block) {
  const tokens = new Map()
  for (const line of block.split('\n')) {
    const stripped = line.split('//')[0].trim()
    const match = stripped.match(/^(--[a-zA-Z0-9-]+)\s*:\s*(.+?);?\s*$/)
    if (match) tokens.set(match[1], match[2].replace(/;$/, '').trim())
  }
  return tokens
}

const themes = {
  light: parseTokens(themeBlock('light')),
  dark: parseTokens(themeBlock('dark')),
}

const lightKeys = [...themes.light.keys()].sort().join(',')
const darkKeys = [...themes.dark.keys()].sort().join(',')
if (lightKeys !== darkKeys) {
  const onlyLight = [...themes.light.keys()].filter((k) => !themes.dark.has(k))
  const onlyDark = [...themes.dark.keys()].filter((k) => !themes.light.has(k))
  fail(`theme token sets differ (light-only: ${onlyLight}; dark-only: ${onlyDark})`)
}

function resolve(value, tokens, depth = 0) {
  if (depth > 10) throw new Error('var() cycle')
  return value.replace(/var\(\s*(--[a-zA-Z0-9-]+)\s*\)/g, (_, name) => {
    const inner = tokens.get(name)
    if (inner === undefined) throw new Error(`undefined token ${name}`)
    return resolve(inner, tokens, depth + 1)
  })
}

function toRgb(hex) {
  let h = hex.replace('#', '')
  if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join('')
  h = h.slice(0, 6)
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
}

function luminance([r, g, b]) {
  const f = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

function ratio(a, b) {
  const hi = Math.max(a, b)
  const lo = Math.min(a, b)
  return (hi + 0.05) / (lo + 0.05)
}

function colorValue(ref, theme) {
  const parse = (resolved) => {
    const hex = resolved.match(/#[0-9a-fA-F]{3,8}/)
    if (hex) return { rgb: toRgb(hex[0]), alpha: 1 }
    const rgb = resolved.match(
      /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:\s*[,/]\s*([\d.]+))?\s*\)/,
    )
    if (rgb) {
      return {
        rgb: [Number(rgb[1]) / 255, Number(rgb[2]) / 255, Number(rgb[3]) / 255],
        alpha: rgb[4] === undefined ? 1 : Number(rgb[4]),
      }
    }
    return null
  }
  if (ref.startsWith('#')) {
    const parsed = parse(ref)
    if (parsed) return parsed
  } else {
    const raw = themes[theme].get(ref)
    if (raw !== undefined) {
      const parsed = parse(resolve(raw, themes[theme]))
      if (parsed) return parsed
    }
  }
  fail(`token ${ref} does not resolve to a colour in theme ${theme}`)
  return { rgb: [0, 0, 0], alpha: 1 }
}

// [foreground, background, kind] — kind 'text' needs 4.5:1, 'ui' needs 3:1.
const PAIRS = [
  ['--ink', '--bg', 'text'],
  ['--ink', '--card', 'text'],
  ['--muted', '--bg', 'text'],
  ['--muted', '--card', 'text'],
  ['--ink', '--soft', 'text'],
  ['--muted', '--soft', 'text'],
  ['--ink', '--track', 'text'],
  ['--muted', '--track', 'text'],
  ['--green-ink', '--card', 'text'],
  ['--green-ink', '--soft', 'text'],
  ['--on-primary', '--green-fill', 'text'],
  ['--on-primary', '--green-dark', 'text'],
  ['--warn-ink', '--warn-bg', 'text'],
  ['--error-ink', '--error-bg', 'text'],
  ['--error-ink', '--card', 'text'],
  ['--sale-ink', '--sale-bg', 'text'],
  ['--spend-ink', '--spend-bg', 'text'],
  ['--credit-ink', '--credit-bg', 'text'],
  ['--on-danger', '--error-ink', 'text'],
  ['--line', '--bg', 'ui'],
  ['--line', '--card', 'ui'],
  ['--line', '--soft', 'ui'],
  ['--line', '--track', 'ui'],
  ['--green-fill', '--card', 'ui'],
  ['--green-ink', '--card', 'ui'],
  ['--sale-ink', '--card', 'ui'],
  ['--warn-ink', '--card', 'ui'],
  ['--focus', '--bg', 'ui'],
  ['--focus', '--card', 'ui'],
  ['--green-ink', '--track', 'ui'],
  ['--muted', '--track', 'ui'],
  ['--band-building', '--track', 'ui'],
  ['--band-developing', '--track', 'ui'],
  ['--band-ready', '--track', 'ui'],
  ['--band-strong', '--track', 'ui'],
  ['--recording-ring', '--card', 'ui'],
]

for (const theme of ['light', 'dark']) {
  for (const [fg, bg, kind] of PAIRS) {
    const need = kind === 'text' ? 4.5 : 3
    const fgColor = colorValue(fg, theme)
    const bgColor = colorValue(bg, theme)
    // Alpha-composite translucent foregrounds (e.g. glow rings) onto the
    // background they are drawn over before measuring.
    const over = fgColor.rgb.map(
      (c, i) => c * fgColor.alpha + bgColor.rgb[i] * (1 - fgColor.alpha),
    )
    const got = ratio(luminance(over), luminance(bgColor.rgb))
    if (got < need) {
      fail(`${theme}: ${fg} on ${bg} is ${got.toFixed(2)}:1, needs ${need}:1`)
    }
  }
}

// ---------------------------------------------------------------------------
// 2. No hard-coded colours outside the theme token blocks.
// ---------------------------------------------------------------------------
const blockRanges = []
for (const theme of ['light', 'dark']) {
  const pattern = new RegExp(`html\\[data-theme=['"]${theme}['"]\\]\\s*\\{[\\s\\S]*?\\n\\}`)
  const match = pattern.exec(css)
  if (match) blockRanges.push([match.index, match.index + match[0].length])
}
const inThemeBlock = (index) => blockRanges.some(([s, e]) => index >= s && index < e)

const noComments = css.replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length))
const colorPattern = /#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)/g
let m
while ((m = colorPattern.exec(noComments)) !== null) {
  if (!inThemeBlock(m.index)) {
    const line = noComments.slice(0, m.index).split('\n').length
    fail(`hard-coded colour outside theme tokens (styles.css:${line}): ${m[0]}`)
  }
}

// ---------------------------------------------------------------------------
// 3. No hard-coded colours in TS/TSX, and no undefined var() references.
// ---------------------------------------------------------------------------
function walk(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}
const codeFiles = walk(srcDir).filter((f) => /\.(ts|tsx)$/.test(f) && !/\.test\./.test(f))
const hexInCode = /#[0-9a-fA-F]{3,8}\b/g
for (const file of codeFiles) {
  const text = readFileSync(file, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
  let hit
  while ((hit = hexInCode.exec(text)) !== null) {
    const line = text.slice(0, hit.index).split('\n').length
    fail(`hard-coded colour in ${file.split('src')[1]}:${line}: ${hit[0]}`)
  }
}

const rootMatch = css.match(/:root\s*\{([\s\S]*?)\n\}/)
const rootTokens = rootMatch ? parseTokens(rootMatch[1]) : new Map()
const defined = new Set([
  ...themes.light.keys(),
  ...themes.dark.keys(),
  ...rootTokens.keys(),
])
for (const name of [...css.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)\s*\)/g)].map((x) => x[1])) {
  if (!defined.has(name)) fail(`undefined token ${name} referenced in styles.css`)
}

if (failures > 0) {
  console.error(`\n${failures} theme/contrast problem(s) — refusing build.`)
  process.exit(1)
}
console.log(`Theme contrast OK: ${PAIRS.length * 2} token pairs pass AA in both themes.`)
