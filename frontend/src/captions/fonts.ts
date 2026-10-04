/**
 * The caption fonts: bundled woff2 files, never fetched from the internet.
 *
 * All three are SIL Open Font License 1.1 (free to bundle and to burn into a
 * video), variable in weight, and cover Hebrew and Latin — so a Hebrew line
 * with an English word, a number or punctuation is drawn in one typeface.
 * The files come from the @fontsource-variable packages in node_modules and
 * are imported as URLs, which both Vite (preview) and Remotion's webpack
 * bundle (render) turn into local asset paths.
 *
 * Each subset is registered with its unicode-range, as Fontsource's own CSS
 * does, so the browser picks the Hebrew file for Hebrew letters and the Latin
 * file for Latin letters within one family.
 */

import heeboHebrew from '@fontsource-variable/heebo/files/heebo-hebrew-wght-normal.woff2'
import heeboLatin from '@fontsource-variable/heebo/files/heebo-latin-wght-normal.woff2'
import heeboLatinExt from '@fontsource-variable/heebo/files/heebo-latin-ext-wght-normal.woff2'
import notoHebrew from '@fontsource-variable/noto-sans-hebrew/files/noto-sans-hebrew-hebrew-wght-normal.woff2'
import notoLatin from '@fontsource-variable/noto-sans-hebrew/files/noto-sans-hebrew-latin-wght-normal.woff2'
import notoLatinExt from '@fontsource-variable/noto-sans-hebrew/files/noto-sans-hebrew-latin-ext-wght-normal.woff2'
import rubikHebrew from '@fontsource-variable/rubik/files/rubik-hebrew-wght-normal.woff2'
import rubikLatin from '@fontsource-variable/rubik/files/rubik-latin-wght-normal.woff2'
import rubikLatinExt from '@fontsource-variable/rubik/files/rubik-latin-ext-wght-normal.woff2'

const HEBREW_RANGE = 'U+0307-0308,U+0590-05FF,U+200C-2010,U+20AA,U+25CC,U+FB1D-FB4F'
const LATIN_RANGE =
  'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,' +
  'U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD'
const LATIN_EXT_RANGE =
  'U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,' +
  'U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF'

type FontDefinition = {
  /** The CSS family name used by the composition. Unique to this app. */
  family: string
  files: { url: string; range: string }[]
}

/** Keys are the backend's `font_family` choices (see backend/captions.py). */
export const CAPTION_FONTS: Record<string, FontDefinition> = {
  heebo: {
    family: 'VF Caption Heebo',
    files: [
      { url: heeboHebrew, range: HEBREW_RANGE },
      { url: heeboLatin, range: LATIN_RANGE },
      { url: heeboLatinExt, range: LATIN_EXT_RANGE },
    ],
  },
  rubik: {
    family: 'VF Caption Rubik',
    files: [
      { url: rubikHebrew, range: HEBREW_RANGE },
      { url: rubikLatin, range: LATIN_RANGE },
      { url: rubikLatinExt, range: LATIN_EXT_RANGE },
    ],
  },
  'noto-sans-hebrew': {
    family: 'VF Caption Noto Sans Hebrew',
    files: [
      { url: notoHebrew, range: HEBREW_RANGE },
      { url: notoLatin, range: LATIN_RANGE },
      { url: notoLatinExt, range: LATIN_EXT_RANGE },
    ],
  },
}

export const DEFAULT_FONT = 'heebo'

export function fontFamily(key: string): string {
  const definition = CAPTION_FONTS[key] ?? CAPTION_FONTS[DEFAULT_FONT]
  // A generic fallback is listed only so a missing glyph is still drawn; the
  // bundled files cover Hebrew and Latin, which is everything validated text
  // is expected to hold.
  return `"${definition.family}", sans-serif`
}

const loading = new Map<string, Promise<void>>()

/**
 * Register and load every subset of one family. Resolves once the browser has
 * the font data, so a frame is never captured in a fallback font.
 */
export function loadCaptionFont(key: string): Promise<void> {
  const definition = CAPTION_FONTS[key] ?? CAPTION_FONTS[DEFAULT_FONT]
  const existing = loading.get(definition.family)
  if (existing) return existing

  const promise = Promise.all(
    definition.files.map(async ({ url, range }) => {
      const face = new FontFace(definition.family, `url(${url}) format('woff2')`, {
        weight: '100 900',
        style: 'normal',
        unicodeRange: range,
      })
      await face.load()
      document.fonts.add(face)
    }),
  ).then(() => undefined)

  loading.set(definition.family, promise)
  return promise
}
