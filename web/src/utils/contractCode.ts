/** Shapes and helpers for code generated from an API contract. */

export type CodeTarget = 'models' | 'angular' | 'react' | 'vue' | 'mocks';

export interface GeneratedCode {
  target: CodeTarget; base_url: string; operations: number; schemas: number;
  files: Array<{ path: string; content: string }>; notes: string[];
}

export const CODE_TARGETS: Array<{ id: CodeTarget; label: string; hint: string }> = [
  { id: 'models', label: 'Types only', hint: 'One TypeScript type per contract schema.' },
  { id: 'angular', label: 'Angular service', hint: 'An injectable service over HttpClient, returning Observables.' },
  { id: 'react', label: 'React hooks', hint: 'Typed fetch functions plus a hook per operation.' },
  { id: 'vue', label: 'Vue composables', hint: 'Typed fetch functions plus a composable per operation.' },
  { id: 'mocks', label: 'Mocks', hint: 'Example responses and Mock Service Worker handlers.' },
];

/** The target that fits a project's framework, as reported by its UI map. */
export const targetForFramework = (framework: string | null | undefined): CodeTarget =>
  framework === 'angular' ? 'angular' : framework === 'react' || framework === 'next' ? 'react'
    : framework === 'vue' || framework === 'nuxt' ? 'vue' : 'models';

export const lineCount = (content: string): number => content.split('\n').length - (content.endsWith('\n') ? 1 : 0);
