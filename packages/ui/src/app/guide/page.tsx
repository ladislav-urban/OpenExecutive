'use client';

import ProductName from "@/components/ProductName";
import { useCallback, useEffect, useRef, useState } from 'react';

import DynamicSection from '@/components/architecture/DynamicSection';
import PageSideNav from '@/components/shell/PageSideNav';
import { GUIDE_SECTIONS, readyToScroll, sectionFromSearch } from '@/lib/guideSections';

// The sections, in order (lib/guideSections.ts: hardcoded so the sidebar
// renders at once, ids checked against the API's registry).
const SECTIONS = GUIDE_SECTIONS;

interface SectionMeta {
  id: string;
  fresh: boolean;
  generated_at: string | null;
}

export default function GuidePage() {
  const [activeSection, setActiveSection] = useState('chat');
  const [sectionMeta, setSectionMeta] = useState<Record<string, SectionMeta>>({});
  const observerRef = useRef<IntersectionObserver | null>(null);

  // `/guide?section=<id>` (a host's Help menu links so) opens at that section:
  // scrolled to once it and everything above it have loaded, so nothing above
  // moves it afterwards. Not if the reader scrolled first.
  const targetRef = useRef<string | null>(null);
  const settledRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    targetRef.current = sectionFromSearch(window.location.search);
    if (!targetRef.current) return;
    const stop = () => {
      targetRef.current = null;
    };
    const keys = (e: KeyboardEvent) => {
      if (['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(e.key)) stop();
    };
    window.addEventListener('wheel', stop, { passive: true, capture: true });
    window.addEventListener('touchmove', stop, { passive: true, capture: true });
    window.addEventListener('keydown', keys, { capture: true });
    return () => {
      window.removeEventListener('wheel', stop, { capture: true });
      window.removeEventListener('touchmove', stop, { capture: true });
      window.removeEventListener('keydown', keys, { capture: true });
    };
  }, []);
  const onSettled = useCallback((id: string) => {
    settledRef.current.add(id);
    const target = targetRef.current;
    if (target && readyToScroll(target, settledRef.current)) {
      targetRef.current = null;
      document.getElementById(target)?.scrollIntoView({ block: 'start' });
      setActiveSection(target);
    }
  }, []);

  // Single cheap listing call — no generation triggered.
  useEffect(() => {
    fetch('/api/backend/guide/sections')
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((data: { sections: SectionMeta[] }) => {
        const map: Record<string, SectionMeta> = {};
        for (const s of data.sections) map[s.id] = s;
        setSectionMeta(map);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    observerRef.current?.disconnect();
    observerRef.current = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setActiveSection(e.target.id);
      },
      { rootMargin: '-20% 0px -70% 0px', threshold: 0 }
    );
    SECTIONS.forEach(({ id }) => {
      const el = document.getElementById(id);
      if (el) observerRef.current?.observe(el);
    });
    return () => observerRef.current?.disconnect();
  }, []);

  const freshCount = Object.values(sectionMeta).filter((s) => s.fresh).length;
  const totalCount = SECTIONS.length;

  return (
    <div className="flex flex-col md:flex-row flex-1 min-h-0 bg-surface text-fg overflow-hidden">
      <PageSideNav
        label="Section"
        current={SECTIONS.find((s) => s.id === activeSection)?.label}
        closeKey={activeSection}
        className="md:w-52 bg-surface-elevated"
      >
        <div className="px-3 py-4">
          <p className="px-2 text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
            User Guide
          </p>
          <nav className="space-y-0.5">
            {SECTIONS.map(({ id, label }) => {
              const meta = sectionMeta[id];
              const dotColor = meta?.fresh ? 'bg-emerald-500/60' : 'bg-surface-input';
              return (
                <a
                  key={id}
                  href={`#${id}`}
                  data-closes-nav
                  onClick={(e) => {
                    e.preventDefault();
                    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                  }}
                  className={`flex items-center gap-2 px-2 py-2.5 md:py-1.5 rounded-lg text-sm md:text-xs transition-colors ${
                    activeSection === id
                      ? 'bg-indigo-500/10 text-indigo-400'
                      : 'text-fg-muted hover:text-fg hover:bg-surface-overlay/60'
                  }`}
                >
                  <span className={`inline-block w-1.5 h-1.5 rounded-full ${dotColor}`} />
                  <span>{label}</span>
                </a>
              );
            })}
          </nav>
        </div>

        <div className="mt-auto px-4 py-4 border-t border-line space-y-1.5">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
            Reference
          </p>
          <div className="flex justify-between text-xs">
            <span className="text-fg-subtle">Features</span>
            <span className="text-fg-muted font-mono">{freshCount} / {totalCount}</span>
          </div>
          <p className="text-[10px] text-fg-subtle leading-relaxed">
            Plain-language overviews of what each feature is and what it does. For how the system is
            built, see the Architecture reference.
          </p>
        </div>
      </PageSideNav>

      <main className="flex-1 min-h-0 min-w-0 overflow-y-auto">
        <div className="max-w-4xl mx-auto px-4 py-6 sm:px-8 sm:py-10 space-y-12 sm:space-y-20">
          <div>
            <h1 className="text-2xl font-bold text-fg"><ProductName /> — User Guide</h1>
            <p className="mt-2 text-sm text-fg-muted">
              A quick tour of every feature: what it is, and what it does for you. Not a manual —
              just enough to know where to go and why. For the technical internals, see the
              Architecture reference.
            </p>
          </div>

          {SECTIONS.map(({ id, label, sub }) => (
            <DynamicSection key={id} id={id} title={label} sub={sub} basePath="guide" onSettled={onSettled} />
          ))}
        </div>
      </main>
    </div>
  );
}
