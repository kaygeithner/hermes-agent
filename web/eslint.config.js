import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

// React 19's compiler-backed react-hooks preset added broad migration rules
// (purity / refs / set-state-in-effect / …) that flag patterns in the existing
// dashboard components. Rather than disable them across the WHOLE package — which
// also blinds every new file — keep them ON by default and turn each off ONLY for
// the specific legacy files that currently trip it. Treat these as burn-down
// allowlists: refactor a component, then delete it from the list. New code (and
// any file not listed) keeps full coverage.
const LEGACY_SET_STATE_IN_EFFECT = [
  'src/components/OAuthProvidersCard.tsx',
  'src/contexts/PageHeaderProvider.tsx',
  'src/pages/AnalyticsPage.tsx',
  'src/pages/ConfigPage.tsx',
  'src/pages/LogsPage.tsx',
  'src/pages/ModelsPage.tsx',
  'src/pages/PluginsPage.tsx',
  'src/pages/SessionsPage.tsx',
  'src/pages/SkillsPage.tsx',
]
const LEGACY_REFS = [
  'src/App.tsx',
  'src/components/LanguageSwitcher.tsx',
  'src/components/OAuthProvidersCard.tsx',
  'src/components/ThemeSwitcher.tsx',
]
const LEGACY_ONLY_EXPORT_COMPONENTS = [
  'src/components/SidebarStatusStrip.tsx',
  'src/i18n/context.tsx',
  'src/themes/context.tsx',
]

const CONFIG_DIR = dirname(fileURLToPath(import.meta.url))

// Build a per-file rule-override block, but FIRST assert every listed path still
// exists. A hand-curated allowlist silently re-enables its rule on a file that
// was moved/renamed (the stale entry then matches nothing), with no signal. Burn-
// down lists only ever shrink, so a missing path is always a stale entry to fix
// here — fail loudly instead. Co-locating the check with the override keeps the
// two from drifting (no separate list to forget to update).
const legacyOff = (files, rules) => {
  for (const file of files) {
    if (!existsSync(resolve(CONFIG_DIR, file))) {
      throw new Error(
        `eslint.config.js: legacy allowlist references a missing file: ${file}. ` +
          'It was likely moved or renamed — update or remove the stale entry ' +
          `(otherwise ${Object.keys(rules).join(', ')} is silently re-enabled on the moved file).`
      )
    }
  }

  return { files, rules }
}

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
  },
  // ── Legacy allowlists — burn down by refactoring, never by widening ────────
  // Each entry asserts its files exist (see legacyOff) so a rename surfaces here
  // instead of silently re-enabling the rule on the moved file.
  legacyOff(LEGACY_SET_STATE_IN_EFFECT, { 'react-hooks/set-state-in-effect': 'off' }),
  legacyOff(LEGACY_REFS, { 'react-hooks/refs': 'off' }),
  legacyOff(LEGACY_ONLY_EXPORT_COMPONENTS, { 'react-refresh/only-export-components': 'off' }),
  legacyOff(['src/App.tsx'], { 'react-hooks/purity': 'off' }),
  legacyOff(['src/pages/ConfigPage.tsx'], { 'react-hooks/preserve-manual-memoization': 'off' }),
  legacyOff(['src/plugins/PluginPage.tsx'], { 'react-hooks/static-components': 'off' }),
])
