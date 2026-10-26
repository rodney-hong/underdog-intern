// OVER / UNDER keep one hue each (--over, --under) everywhere a pick or a result is shown:
// History's Predicted and Actual, and on Predict the result card, the screenshot table's Result
// and the slip legs. Anything else (Pending, DNP, an error) gets no colour.
export const sideColor = (side) => (side === 'OVER' ? 'var(--over)' : side === 'UNDER' ? 'var(--under)' : undefined)
