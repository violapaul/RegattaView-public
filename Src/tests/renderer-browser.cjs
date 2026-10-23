// Real WebGL regression test: a decoded frame must produce visible canvas pixels.
// Run with: node tests/renderer-browser.cjs (isolated installed Chrome).
const {chromium} = require('playwright-core');
const webpack = require('webpack');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
    const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'regattaview-renderer-'));
    let browser;
    try {
        await new Promise((resolve, reject) => {
            const compiler = webpack({mode:'production', context:path.resolve(__dirname, '..'),
                entry:'./src/js/fisheye_renderer.js', devtool:false,
                output:{path:temporary, filename:'renderer.js', library:{name:'RendererUnderTest',type:'var'}}});
            compiler.run((error, stats) => compiler.close(() => {
                if (error || stats.hasErrors()) reject(error || new Error(stats.toString({all:false,errors:true})));
                else resolve();
            }));
        });
        browser = await chromium.launch({channel:'chrome',headless:true});
        const page = await browser.newPage();
        await page.setContent('<canvas id="output" width="64" height="64"></canvas>');
        await page.addScriptTag({path:path.join(temporary, 'renderer.js')});
        const results = await page.evaluate(() => {
            const gl = document.getElementById('output').getContext('webgl');
            if (!gl) throw new Error('WebGL unavailable in this test browser');
            const renderer = RendererUnderTest.createFisheyeRenderer(gl);
            const source = document.createElement('canvas');
            const result = [];
            function sample(label, rgb, width, height, projection) {
                if (rgb) {
                    source.width = width; source.height = height;
                    const ctx = source.getContext('2d');
                    ctx.fillStyle = `rgb(${rgb.join(',')})`; ctx.fillRect(0,0,width,height);
                }
                renderer.setProjection(projection);
                renderer.draw({source:rgb ? source : null,width:64,height:64});
                const pixel = new Uint8Array(4);
                gl.readPixels(32,32,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixel);
                result.push({label,pixel:Array.from(pixel),error:gl.getError(),expected:rgb || [20,30,40]});
            }
            sample('private-photo-free placeholder',null,0,0,'fisheye');
            sample('3840x1920 fisheye frame',[240,80,30],3840,1920,'fisheye');
            sample('updated fisheye frame',[30,200,80],3840,1920,'fisheye');
            sample('1920x960 equirectangular frame',[40,90,230],1920,960,'equirect');
            sample('projection switch back to fisheye',[170,120,60],3840,1920,'fisheye');
            return result;
        });
        for (const result of results) {
            assert.equal(result.error,0,result.label + ': WebGL error');
            result.expected.forEach((value,index) => assert.ok(Math.abs(result.pixel[index]-value)<=2,
                `${result.label}: expected ${result.expected}, rendered ${result.pixel}`));
            console.log('PASS ' + result.label);
        }
    } finally {
        if (browser) await browser.close();
        await fs.rm(temporary,{recursive:true,force:true});
    }
})().catch(error => {console.error(error);process.exitCode=1;});
