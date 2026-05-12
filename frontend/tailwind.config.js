/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        court: {
          bg: '#0d0f14',
          card: '#161a23',
          border: '#252b38',
          muted: '#4b5563',
          accent: '#3b82f6',
        },
      },
    },
  },
  plugins: [],
}
