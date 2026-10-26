import { useEffect, useState } from 'react'

// Phones get stacked cards instead of wide tables. Tailwind's sm breakpoint is 640 px.
export const PHONE_QUERY = '(max-width: 639px)'

// true while the media query matches; false where matchMedia doesn't exist (jsdom tests)
export function useMediaQuery(query) {
  const get = () => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(query).matches : false)
  const [matches, setMatches] = useState(get)
  useEffect(() => {
    if (!window.matchMedia) return undefined
    const mq = window.matchMedia(query)
    const onChange = () => setMatches(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [query])
  return matches
}

export const useIsPhone = () => useMediaQuery(PHONE_QUERY)
