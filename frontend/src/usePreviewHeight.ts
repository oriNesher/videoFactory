import { useEffect, useState, type RefObject } from 'react'

/**
 * The shared preview panel's height, so a table beside it can be exactly as
 * tall. Null until measured (outside the stage carousel, for instance).
 */
export function usePreviewHeight(anchor: RefObject<HTMLElement | null>): number | null {
  const [height, setHeight] = useState<number | null>(null)
  useEffect(() => {
    const preview = anchor.current?.closest('.stage-body')?.querySelector<HTMLElement>('.video-preview')
    if (!preview) return
    const observer = new ResizeObserver(() => setHeight(preview.getBoundingClientRect().height))
    observer.observe(preview)
    return () => observer.disconnect()
  }, [anchor])
  return height
}
