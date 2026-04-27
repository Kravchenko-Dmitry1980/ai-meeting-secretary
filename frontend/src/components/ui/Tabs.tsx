import { cn } from '../../utils/cn';

interface TabsProps {
  tabs: string[];
  active: string;
  onChange: (tab: string) => void;
}

export const Tabs = ({ tabs, active, onChange }: TabsProps) => (
  <div className="flex flex-wrap gap-2 rounded-2xl border border-white/10 bg-black/20 p-2">
    {tabs.map((tab) => (
      <button
        key={tab}
        className={cn(
          'rounded-xl px-3 py-2 text-sm transition',
          active === tab
            ? 'bg-violet-500 text-white shadow-glow'
            : 'bg-transparent text-slate-300 hover:bg-white/5',
        )}
        onClick={() => onChange(tab)}
        type="button"
      >
        {tab}
      </button>
    ))}
  </div>
);
