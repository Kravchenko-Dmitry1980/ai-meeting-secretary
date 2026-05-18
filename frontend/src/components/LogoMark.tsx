import { motion } from 'framer-motion';

export const LogoMark = () => (
  <div className="relative flex h-11 w-11 items-center justify-center rounded-2xl bg-white/10">
    <motion.div
      animate={{ rotate: 360 }}
      className="absolute h-10 w-10 rounded-2xl border border-violet-300/50"
      transition={{ duration: 8, repeat: Infinity, ease: 'linear' }}
    />
    <span className="text-sm font-bold tracking-wide text-violet-200">AIS</span>
  </div>
);
