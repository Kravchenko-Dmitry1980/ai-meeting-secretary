// Adapted from owner's ai-meeting-secretary, commit 0268c2bd. See REUSE.md.
import type { ButtonHTMLAttributes } from 'react';
import { cn } from '../../utils/cn';

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'ghost' | 'danger' };
export const Button = ({ className, variant = 'primary', type = 'button', ...props }: Props) => (
  <button type={type} className={cn(
    'button rounded-xl px-4 py-2.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-40',
    variant === 'primary'
      ? 'bg-gradient-to-r from-violet-500 to-fuchsia-500 text-white shadow-[0_8px_24px_rgba(139,92,246,0.2)] hover:brightness-110'
      : variant === 'danger' ? 'border border-rose-400/30 bg-rose-400/10 text-rose-200 hover:bg-rose-400/20'
      : 'border border-white/10 bg-white/[0.03] text-slate-200 hover:bg-white/[0.07]',
    className,
  )} {...props} />
);
