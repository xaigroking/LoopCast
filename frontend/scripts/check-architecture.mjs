import ts from 'typescript';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../src');
const files = [];
async function scan(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) await scan(path);
    else if (/\.tsx?$/.test(path)) files.push(path);
  }
}
await scan(root);
const graph = new Map(files.map((file) => [file, new Set()]));
for (const file of files) {
  const tree = ts.createSourceFile(file, await readFile(file, 'utf8'), ts.ScriptTarget.Latest, true);
  function visit(node) {
    let specifier;
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) specifier = node.moduleSpecifier;
    else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) specifier = node.arguments[0];
    if (specifier && ts.isStringLiteral(specifier)) {
      const name = specifier.text;
      const serverCrypto = relative(root, file) === 'proxy.ts' && name === 'node:crypto';
      if ((!serverCrypto && /^(node:|fs$|sqlite|better-sqlite|pg$)/.test(name)) || name.includes('loopcast/')) throw Error(`Backend dependency in ${relative(root, file)}`);
      if (name.startsWith('.') || name.startsWith('@/')) {
        const path = name.startsWith('@/') ? resolve(root, name.slice(2)) : resolve(dirname(file), name);
        if (!path.startsWith(root + '/') && !path.includes('/public/locales/')) throw Error(`Source boundary crossed by ${relative(root, file)}: ${name}`);
        const target = [path, path + '.ts', path + '.tsx', join(path, 'index.ts')].find((item) => graph.has(item));
        if (target) {
          if (relative(root, file).startsWith('lib/') && /^(app|components)\//.test(relative(root, target))) throw Error(`Library imports UI: ${relative(root, file)}`);
          graph.get(file).add(target);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
}
const active = new Set(), visited = new Set();
function visit(file) {
  if (active.has(file)) throw Error(`Frontend import cycle at ${relative(root, file)}`);
  if (visited.has(file)) return;
  active.add(file);
  for (const target of graph.get(file)) visit(target);
  active.delete(file); visited.add(file);
}
for (const file of files) visit(file);
console.log(`Frontend architecture passed: ${files.length} files, acyclic imports and UI/API boundaries`);
