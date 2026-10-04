import type { CaptionStyle } from './captions/types'
import type { CaptionCatalog, CaptionSpec } from './types'

/**
 * One caption template, a handful of controls. Driven by the backend's style
 * spec, so the ranges here are the ranges the backend validates. Changes show
 * in the preview at once; they reach an export only once saved.
 */

// Controls that only matter for one readability mode.
const ONLY_WHEN: Record<string, CaptionStyle['background']> = {
  outline_color: 'outline',
  outline_width_percent: 'outline',
  box_color: 'box',
  box_opacity: 'box',
}

type Props = {
  catalog: CaptionCatalog
  style: CaptionStyle
  onChange: (style: CaptionStyle) => void
}

export default function CaptionStyleForm({ catalog, style, onChange }: Props) {
  const values = style as unknown as Record<string, unknown>
  const fontLabels = new Map(catalog.fonts.map((font) => [font.id, font.label]))

  function set(name: string, value: unknown) {
    onChange({ ...style, [name]: value } as CaptionStyle)
  }

  function control(spec: CaptionSpec) {
    const value = values[spec.name]
    switch (spec.type) {
      case 'choice':
        return (
          <select value={String(value)} onChange={(event) => set(spec.name, event.target.value)}>
            {(spec.choices ?? []).map((choice) => (
              <option key={choice} value={choice}>
                {fontLabels.get(choice) ?? choice}
              </option>
            ))}
          </select>
        )
      case 'colour':
        return (
          <input type="color" value={String(value)} onChange={(event) => set(spec.name, event.target.value)} />
        )
      case 'boolean':
        return (
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(event) => set(spec.name, event.target.checked)}
          />
        )
      default:
        return (
          <span className="row">
            <input
              type="range"
              min={spec.min}
              max={spec.max}
              step={spec.step}
              value={Number(value)}
              onChange={(event) => set(spec.name, Number(event.target.value))}
            />
            <span className="small mono">{Number(value)}</span>
          </span>
        )
    }
  }

  return (
    <div className="caption-style-grid">
      {catalog.style
        .filter((spec) => !ONLY_WHEN[spec.name] || ONLY_WHEN[spec.name] === style.background)
        .filter((spec) => spec.name !== 'highlight_color' || style.highlight_active_word)
        .map((spec) => (
          <label key={spec.name} className="field">
            <span>
              {spec.label}
              {spec.unit && <span className="small"> ({spec.unit})</span>}
            </span>
            {control(spec)}
          </label>
        ))}
    </div>
  )
}
