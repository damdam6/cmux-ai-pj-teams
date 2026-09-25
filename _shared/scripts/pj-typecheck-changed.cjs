#!/usr/bin/env node
// File-scoped TypeScript diagnostics using the repository's own compiler/config.
// No emit, build, whole-program diagnostics, or package-manager commands.
const fs = require('node:fs');
const path = require('node:path');

function checkChanged(root, files) {
  root = path.resolve(root);
  const targets = [...new Set(files.map(file => path.resolve(root, file)))]
    .filter(file => /\.(?:[cm]?ts|tsx)$/.test(file));
  if (!targets.length) return { status: 'none', reason: '변경된 TypeScript 파일 없음', checked: [] };
  const groups = new Map();
  const cache = new Map();
  const relative = file => path.relative(root, file);
  for (const file of targets) {
    if (relative(file).startsWith('..' + path.sep) || path.isAbsolute(relative(file))) {
      throw new Error(`저장소 밖 경로: ${file}`);
    }
    if (!fs.statSync(file).isFile()) throw new Error(`검사 파일 없음: ${relative(file)}`);
    let compiler;
    try {
      compiler = require.resolve('typescript', { paths: [path.dirname(file), root] });
    } catch {
      throw new Error(`저장소의 typescript 의존성을 찾을 수 없음: ${relative(file)}`);
    }
    const ts = require(compiler);
    function parse(config) {
      const key = compiler + '\0' + config;
      if (!cache.has(key)) {
        const fatal = [];
        const parsed = ts.getParsedCommandLineOfConfigFile(config, {}, {
          ...ts.sys, onUnRecoverableConfigFileDiagnostic: d => fatal.push(d),
        });
        const errors = [...fatal, ...(parsed?.errors || [])];
        // An empty referenced project may have no input; it is not a target.
        const relevant = errors.filter(d => d.code !== 18003);
        if (!parsed || relevant.length) {
          throw new Error(`tsconfig 읽기 실패 (${relative(config)}): ` +
            relevant.map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n')).join('\n'));
        }
        cache.set(key, parsed);
      }
      return cache.get(key);
    }
    function candidates(config, seen = new Set()) {
      config = path.resolve(config);
      if (seen.has(config)) return [];
      seen.add(config);
      const parsed = parse(config);
      // Prefer a matching referenced app/node project over a solution config.
      const children = (parsed.projectReferences || []).flatMap(ref =>
        candidates(ts.resolveProjectReferencePath(ref), seen));
      const own = parsed.fileNames.some(name => path.resolve(name) === file) ? [config] : [];
      return children.length ? children : own;
    }
    let directory = path.dirname(file);
    let selected = [];
    while (true) {
      const config = path.join(directory, 'tsconfig.json');
      if (fs.existsSync(config)) {
        selected = candidates(config);
        // Explicitly check changed files even if the nearest simple config excludes them.
        if (!selected.length && !(parse(config).projectReferences || []).length) selected = [config];
        if (!selected.length) throw new Error(`변경 파일에 대응하는 tsconfig 참조 없음: ${relative(file)}`);
        break;
      }
      if (directory === root) break;
      const parent = path.dirname(directory);
      if (parent === directory || !directory.startsWith(root + path.sep)) break;
      directory = parent;
    }
    if (!selected.length) throw new Error(`tsconfig.json을 찾을 수 없음: ${relative(file)}`);
    for (const config of selected) {
      const key = compiler + '\0' + config;
      if (!groups.has(key)) groups.set(key, { ts, config, parsed: parse(config), files: [] });
      groups.get(key).files.push(file);
    }
  }
  const checked = [];
  const diagnostics = [];
  for (const { ts, config, parsed, files: changed } of groups.values()) {
    const options = {
      ...parsed.options, noEmit: true, noCheck: false,
      composite: false, incremental: false, tsBuildInfoFile: undefined,
      emitDeclarationOnly: false,
      skipLibCheck: changed.some(file => /\.d\.[cm]?ts$/.test(file)) ? false : parsed.options.skipLibCheck,
    };
    // Ambient declarations preserve globals; imports/type dependencies are resolved normally.
    // Unchanged implementation files are not added as program roots or diagnosed.
    const ambient = parsed.fileNames.filter(file => /\.d\.[cm]?ts$/.test(file));
    const program = ts.createProgram({ rootNames: [...new Set([...changed, ...ambient])], options });
    const errors = [...program.getOptionsDiagnostics(), ...program.getGlobalDiagnostics()];
    for (const file of changed) {
      const source = program.getSourceFile(file);
      if (!source) throw new Error(`TypeScript가 변경 파일을 읽지 못함: ${relative(file)}`);
      errors.push(...program.getSyntacticDiagnostics(source), ...program.getSemanticDiagnostics(source));
      checked.push({ file: relative(file), config: relative(config) });
    }
    for (const diagnostic of errors) {
      if (diagnostic.category !== ts.DiagnosticCategory.Error) continue;
      const location = diagnostic.file && diagnostic.start !== undefined
        ? diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start) : undefined;
      diagnostics.push({
        file: diagnostic.file ? relative(diagnostic.file.fileName) : undefined,
        line: location ? location.line + 1 : undefined,
        column: location ? location.character + 1 : undefined,
        code: diagnostic.code,
        message: ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'),
      });
    }
  }
  return { status: diagnostics.length ? 'fail' : 'ok', checked, diagnostics };
}

if (require.main === module) {
  try {
    const { root, files } = JSON.parse(fs.readFileSync(0, 'utf8'));
    const result = checkChanged(root, files);
    console.log(JSON.stringify(result, null, 2));
    process.exitCode = { ok: 0, fail: 1, none: 3 }[result.status];
  } catch (error) {
    console.log(JSON.stringify({ status: 'unparsed', reason: error.message }, null, 2));
    process.exitCode = 2;
  }
}
module.exports = { checkChanged };
