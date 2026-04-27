import type { ButtonHTMLAttributes } from 'react';
import { cn } from '../../utils/cn';

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'ghost';
};

export const Button = ({ className, variant = 'primary', ...props }: Props) => (
  <button
    className={cn(
      'rounded-xl px-4 py-2 text-sm font-semibold transition-all duration-300 disabled:cursor-not-allowed disabled:opacity-40',
      variant === 'primary'
        ? 'bg-violet-500 text-white shadow-glow hover:-translate-y-0.5 hover:bg-violet-400'
        : 'border border-white/15 bg-white/5 text-white hover:bg-white/10',
      className,
    )}
    {...props}
  />
);
