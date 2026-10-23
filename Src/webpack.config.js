const path = require('path');
const webpack = require('webpack');
const HtmlWebpackPlugin = require('html-webpack-plugin');
const CopyPlugin = require('copy-webpack-plugin');
const MiniCssExtractPlugin = require('mini-css-extract-plugin');

const FAVICON = path.resolve(__dirname, 'src/assets/favicon.svg');

module.exports = {
    // mode set via CLI: npm run build uses --mode=production, dev server defaults to development
    // Define entry points based on the HTML files that need specific JS
    entry: {
        graphs: './src/js/graphs.js',
        index: './src/js/index.js',
        sailview: './src/js/sailview.js',
        login: './src/js/login.js',
        calibrate: './src/js/calibrate.js',
        playlists: './src/js/playlists.js',
        review: './src/js/review.js',
        tracks: './src/js/tracks.js',
        label: './src/js/label.js',
        lesson: './src/js/lesson.js',
        lessons: './src/js/lessons.js',
        notes: './src/js/notes.js',
    },
    output: {
        filename: 'js/[name]_pack.js', // Output JS files to js/ subdirectory
        path: path.resolve(__dirname, 'dist'), // Output to dist/ directory
        publicPath: 'auto', // Automatically determine base path for assets
        clean: true, // Clean the dist directory before each build
    },
    module: {
        rules: [
            {
                test: /\.css$/i,
                use: [MiniCssExtractPlugin.loader, 'css-loader'], // Extract CSS into files
            },
            // Removed HTML loader rule, handled by HtmlWebpackPlugin
            { // Rule for font files
                test: /\.(woff|woff2|eot|ttf|otf)$/i,
                type: 'asset/resource',
                generator: {
                    filename: 'assets/fonts/[name][ext]' // Output fonts to assets/fonts/
                }
            },
            { // Rule for image files in CSS/JS
                test: /\.(png|svg|jpg|jpeg|gif)$/i,
                type: 'asset/resource',
                generator: {
                  filename: 'assets/images/[name][ext]' // Output images to assets/images/
                }
            },
        ],
    },
    plugins: [
        // MathJax otherwise evals Node's require at startup to discover its version.
        // Supply its own build-time constant so graph pages work under strict CSP.
        new webpack.DefinePlugin({PACKAGE_VERSION: JSON.stringify(require('mathjax-full/package.json').version)}),
        new HtmlWebpackPlugin({template: './src/html/privacy.html', filename: 'privacy.html', chunks: []}),
        new MiniCssExtractPlugin({
            filename: 'css/[name].css', // Output CSS to css/ subdirectory
        }),
        new CopyPlugin({
            patterns: [
                { from: 'src/assets', to: 'assets', globOptions: {
                    // Private historical photos remain in repo-data, never in a build.
                    ignore: ['**/images/**', '**/.DS_Store'],
                } },
                // CSS is handled by MiniCssExtractPlugin, no need to copy separately unless there are other files in src/css
            ],
        }),
        // Generate HTML files, injecting corresponding JS bundles
        new HtmlWebpackPlugin({
            template: './src/html/graphs.html',
            filename: 'graphs.html',
            chunks: ['graphs'], // Include the 'graphs' JS chunk
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/index.html',
            filename: 'index.html',
            chunks: ['index'], // Include the 'index' JS chunk
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/sailview.html',
            filename: 'sailview.html',
            chunks: ['sailview'], // Include the 'sailview' JS chunk
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/login.html',
            filename: 'login.html',
            chunks: ['login'], // Include the 'login' JS chunk
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/calibrate.html',
            filename: 'calibrate.html',
            chunks: ['calibrate'],
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/playlists.html',
            filename: 'playlists.html',
            chunks: ['playlists'],
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/review.html',
            filename: 'review.html',
            chunks: ['review'],
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/tracks.html',
            filename: 'tracks.html',
            chunks: ['tracks'],
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/label.html',
            filename: 'label.html',
            chunks: ['label'],
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/lesson.html',
            filename: 'lesson.html',
            chunks: ['lesson'],
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/lessons.html',
            filename: 'lessons.html',
            chunks: ['lessons'],
            favicon: FAVICON,
        }),
        new HtmlWebpackPlugin({
            template: './src/html/notes.html',
            filename: 'notes.html',
            chunks: ['notes'],
            favicon: FAVICON,
        }),
    ],
    resolve: {
        // Bound formatter precision/width until upstream ships a fix for
        // GHSA-hp3w-g68c-fv3c. The exact alias leaves the wrapper's core import intact.
        alias: {'sprintf-js$': path.resolve(__dirname, 'src/js/bounded_sprintf.cjs')},
    },
    // Optional: Configure optimization for production builds
    // optimization: {
    //    splitChunks: {
    //        chunks: 'all',
    //    },
    // },
    devtool: 'source-map', // Generate source maps for easier debugging
    // The graphs entry intentionally bundles Bokeh (~1.4 MiB), and the
    // sailview entry is dominated by gl-matrix + twgl + leaflet — both are
    // internal tools, never on a bandwidth-constrained path. Disable
    // webpack's "your bundle is big" hints so they don't drown the console.
    performance: {
        hints: false,
    },
};

