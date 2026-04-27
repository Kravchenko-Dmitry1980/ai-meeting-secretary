import type { HTMLAttributes } from 'react';
import { cn } from '../../utils/cn';

export const Card = ({ className, ...props }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('glass rounded-2xl p-5', className)} {...props} />
);
