// Palette adapted from the owner's ai-meeting-secretary, commit 0268c2bd.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: { extend: {
    colors: { bg: '#10101b', panel: '#191925', accent: '#a78bfa', mint: '#6ee7b7' },
    boxShadow: { glow: '0 0 40px rgba(139,92,246,0.12)' },
  } },
  plugins: [],
};
