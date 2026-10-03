// Adapted from owner's ai-meeting-secretary, commit 0268c2bd. See REUSE.md.
import { cn } from '../../utils/cn';
interface TabsProps { tabs: string[]; active: string; onChange: (tab: string) => void }
export const Tabs = ({ tabs, active, onChange }: TabsProps) => (
  <div aria-label="Содержимое встречи" role="tablist" className="tabs flex flex-wrap gap-1 rounded-xl border border-white/10 bg-black/15 p-1">
    {tabs.map((tab) => <button
      key={tab} role="tab" aria-selected={active === tab} tabIndex={active === tab ? 0 : -1} type="button"
      className={cn('rounded-lg px-4 py-2.5 text-sm transition', active === tab ? 'bg-violet-500/15 text-violet-200 shadow-glow' : 'text-slate-400 hover:bg-white/5 hover:text-slate-200')}
      onClick={() => onChange(tab)}
      onKeyDown={(event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = tabs.indexOf(tab);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        onChange(tabs[next]);
        event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role=tab]')[next]?.focus();
      }}
    >{tab}</button>)}
  </div>
);
