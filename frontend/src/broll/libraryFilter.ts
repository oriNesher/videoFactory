/**
 * Filtering for the library page: the "description needed" queue and the
 * search over everything else. Matches only what is shown on the page — the
 * user's own words, the file name and the media type.
 */
import type { BrollLibraryAsset } from '../types'

export type LibraryFilter = {
  query: string
  mediaType: 'all' | 'image' | 'video'
  /** `attention`: a file that is missing, changed or unsupported. */
  status: 'all' | 'attention'
}

export const NO_FILTER: LibraryFilter = { query: '', mediaType: 'all', status: 'all' }

/** Assets planning cannot choose yet because nothing says what they show. */
export function needsDescription(assets: BrollLibraryAsset[]): BrollLibraryAsset[] {
  return assets.filter((asset) => !asset.described)
}

export function filterAssets(assets: BrollLibraryAsset[], filter: LibraryFilter): BrollLibraryAsset[] {
  const words = filter.query.toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return assets.filter((asset) => {
    if (filter.mediaType !== 'all' && asset.media_type !== filter.mediaType) return false
    if (filter.status === 'attention' && asset.status === 'available') return false
    if (words.length === 0) return true
    const haystack = [
      asset.display_name,
      asset.filename,
      asset.description ?? '',
      ...asset.tags,
      ...asset.moments.map((moment) => moment.description),
    ]
      .join('\n')
      .toLocaleLowerCase()
    return words.every((word) => haystack.includes(word))
  })
}
