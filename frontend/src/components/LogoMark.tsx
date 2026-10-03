// Adapted from owner's ai-meeting-secretary LogoMark. See REUSE.md.
import { AudioLines } from 'lucide-react';
export const LogoMark = () => (
  <div className="logo-mark relative flex h-10 w-10 items-center justify-center rounded-xl bg-violet-400/10">
    <div className="absolute inset-0 rounded-xl border border-violet-300/30" />
    <AudioLines aria-hidden="true" size={21} className="text-violet-200" />
  </div>
);
