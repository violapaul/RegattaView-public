const google = require('eslint-config-google');
const globals = require('globals');
const parser = require('@typescript-eslint/parser');
const plugin = require('@typescript-eslint/eslint-plugin');
const rules = {...google.rules};
// ESLint 9 removed these core rules; retain the rest of the prior style config.
delete rules['valid-jsdoc'];
delete rules['require-jsdoc'];
module.exports = [{ignores: ['dist/**', 'node_modules/**', '.local-data/**']}, {
    files: ['src/js/**/*.js'],
    languageOptions: {parser, ecmaVersion: 'latest', sourceType: 'module',
        globals: {...globals.browser, ...globals.node}},
    plugins: {'@typescript-eslint': plugin},
    rules: {...rules, indent: ['error', 4], 'max-len': ['error', {code: 120}], 'no-undef': 'error'},
}];
