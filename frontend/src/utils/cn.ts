// Reused from owner's ai-meeting-secretary, commit 0268c2bd. See REUSE.md.
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export const cn = (...inputs: ClassValue[]): string => twMerge(clsx(inputs));
