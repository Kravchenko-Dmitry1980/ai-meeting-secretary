import type { ButtonHTMLAttributes } from 'react';
import { cn } from '../../utils/cn';

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'ghost';
};

export const Button = ({ className, variant = 'primary', ...props }: Props) => (
  <button
    className={cn(
      'rounded-2xl px-6 py-3 text-sm font-semibold tracking-wide transition-all duration-300 disabled:cursor-not-allowed disabled:opacity-40',
      variant === 'primary'
        ? 'bg-gradient-to-r from-violet-500 to-fuchsia-500 text-white shadow-[0_12px_40px_rgba(139,92,246,0.45)] hover:-translate-y-0.5 hover:shadow-[0_16px_50px_rgba(217,70,239,0.45)]'
        : 'border border-white/20 bg-white/5 text-white shadow-[0_8px_24px_rgba(15,23,42,0.35)] hover:bg-white/10 hover:shadow-[0_10px_28px_rgba(15,23,42,0.45)]',
      className,
    )}
    {...props}
  />
);
