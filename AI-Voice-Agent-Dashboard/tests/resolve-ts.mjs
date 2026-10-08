/**
 * Module resolution for `node --test`, matching what Next.js/tsconfig already do.
 *
 * The app's source uses two things Node's ESM resolver does not do on its own:
 *
 *   import { x } from './call-provider'   // no file extension
 *   import { y } from '@/lib/verticals'   // tsconfig "paths" alias
 *
 * Changing the source to satisfy Node would mean changing it to something the
 * bundler does not want, so the test runner is taught the same two rules
 * instead. Nothing here affects the build — it is loaded only via --import.
 */
import { registerHooks } from 'node:module';
import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve as resolvePath } from 'node:path';

const ROOT = dirname(fileURLToPath(new URL('.', import.meta.url)));

/** './x' -> './x.ts' | './x.tsx' | './x/index.ts', first that exists. */
function withExtension(absPath) {
  if (existsSync(absPath) && !absPath.endsWith('/')) {
    // A real file already (e.g. an explicit .ts import).
    return absPath;
  }
  for (const candidate of [
    `${absPath}.ts`,
    `${absPath}.tsx`,
    `${absPath}.mjs`,
    `${absPath}.js`,
    resolvePath(absPath, 'index.ts'),
    resolvePath(absPath, 'index.tsx'),
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    // tsconfig paths: "@/*" -> "<project root>/*"
    if (specifier.startsWith('@/')) {
      const hit = withExtension(resolvePath(ROOT, specifier.slice(2)));
      if (hit) return { url: pathToFileURL(hit).href, shortCircuit: true };
    }

    // Extensionless relative imports.
    if (specifier.startsWith('./') || specifier.startsWith('../')) {
      const parentPath = context.parentURL?.startsWith('file:')
        ? dirname(fileURLToPath(context.parentURL))
        : ROOT;
      const hit = withExtension(resolvePath(parentPath, specifier));
      if (hit) return { url: pathToFileURL(hit).href, shortCircuit: true };
    }

    return nextResolve(specifier, context);
  },
});
