// Adapted from owner's ai-meeting-secretary, commit 0268c2bd. See REUSE.md.
import type { HTMLAttributes } from 'react';
import { cn } from '../../utils/cn';
export const Badge = ({ className, ...props }: HTMLAttributes<HTMLSpanElement>) => (
  <span className={cn('inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-slate-300', className)} {...props} />
);
