import test from 'node:test';
import assert from 'node:assert/strict';
import formatter from '../src/js/bounded_sprintf.cjs';
test('Bokeh numeric formats preserve their output', () => {
    assert.equal(formatter.sprintf('%.1f kn', 6.28), '6.3 kn');
    assert.equal(formatter.sprintf('#%06x', 255), '#0000ff');
    assert.equal(formatter.vsprintf('%0.3e %%', [0.001]), '1.000e-3 %');
});
test('excessive precision, width and malformed formats cannot crash plots', () => {
    for (const format of ['%.999999999f', '%999999999s', '%.0g', '%999999999$d', '%q', 'x'.repeat(1025)]) {
        assert.equal(formatter.sprintf(format, 1.23), '[invalid format]');
    }
    assert.equal(formatter.sprintf('%%.999999999f', 1.23), '%.999999999f');
});
