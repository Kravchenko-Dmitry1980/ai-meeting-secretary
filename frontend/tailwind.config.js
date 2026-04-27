/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#09090f',
        panel: '#111126',
        accent: '#8b5cf6',
        mint: '#34d399',
      },
      boxShadow: {
        glow: '0 0 80px rgba(139, 92, 246, 0.25)',
      },
      animation: {
        'pulse-soft': 'pulseSoft 3s ease-in-out infinite',
      },
      keyframes: {
        pulseSoft: {
          '0%, 100%': { opacity: '0.5' },
          '50%': { opacity: '1' },
        },
      },
    },
  },
  plugins: [],
}

