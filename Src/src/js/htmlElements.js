
/**
 * Adds a slider to element
 * @param {*} element
 * @param {*} name
 * @param {*} min
 * @param {*} max
 * @param {*} defaultValue
 * @param {*} onChange
 * @returns {*}
 */
export function addSlider(element, name, min, max, defaultValue, onChange) {
    //
    const res = {};
    let node = document.createElement('div');
    element.appendChild(node);
    node.appendChild(document.createTextNode(name + ': '));

    res.valueNode = document.createElement('SPAN');
    node.appendChild(res.valueNode);

    node = document.createElement('div');
    element.appendChild(node);
    res.inputNode = document.createElement('INPUT');
    res.inputNode.setAttribute('type', 'range');
    res.inputNode.setAttribute('min', min);
    res.inputNode.setAttribute('max', max);
    res.inputNode.setAttribute('value', defaultValue);
    node.appendChild(res.inputNode);

    res.value = defaultValue;
    res.inputNode.addEventListener('input', function() {
        res.value = res.inputNode.value;
        res.valueNode.innerText = res.value;
        onChange(res.value);
    });
    return res;
}

/**
 * Adds a dropdown to element
 * @param {*} element
 * @param {*} name
 * @param {*} values
 * @param {*} defaultValue
 * @param {*} onChange
 * @returns {*}
 */
export function addDropdown(element, name, values, defaultValue, onChange) {
    const res = {};
    let node = document.createElement('div');
    element.appendChild(node);
    node.appendChild(document.createTextNode(name + ': '));

    res.valueNode = document.createElement('SPAN');
    node.appendChild(res.valueNode);

    node = document.createElement('div');
    element.appendChild(node);
    res.inputNode = document.createElement('select');
    values.forEach((v, index) => {
        const opt = document.createElement('option');
        opt.value = index;
        opt.text = v;
        res.inputNode.add(opt);
    });
    node.appendChild(res.inputNode);
    res.inputNode.addEventListener('input', function() {
        res.index = res.inputNode.selectedIndex;
        res.value = res.inputNode.options[res.index].value;
        res.valueNode.innerText = res.value;
        onChange(res.index);
    });
    res.selectByIndex = (index) => {
        res.inputNode.value = index;
    };
    return res;
}

/**
 * Add attributes to an HTML node from a dictionary.
 * @param {*} node
 * @param {*} dict
 */
function addAttributes(node, dict) {
    Object.entries(dict).forEach(([key, value]) => {
        node.setAttribute(key, value);
    });
}

let _dataFieldCounter = 0;

/**
 * Adds a data field to element. A data field shows a channel selector dropdown,
 * the current value, a sparkline, and a remove button.
 * @param {HTMLElement} element - container to append the row to
 * @param {string} channel - initially selected channel key
 * @param {Array<{key: string, label: string}>} availableChannels - options for the dropdown
 * @param {function(string)} onChange - called with the new channel key when selection changes
 * @param {function()} onRemove - called when the × button is clicked
 * @returns {{channel: string, span: HTMLElement, sparkline: SVGElement, rowEl: HTMLElement}}
 */
export function addDataField(element, channel, availableChannels, onChange, onRemove) {
    const id = _dataFieldCounter++;
    const res = {channel};

    const lineNode = document.createElement('div');
    lineNode.setAttribute('id', 'data_row_' + id);
    lineNode.setAttribute('class', 'data');
    element.appendChild(lineNode);
    res.rowEl = lineNode;

    // Channel selector dropdown
    const select = document.createElement('select');
    select.setAttribute('class', 'data-select');
    availableChannels.forEach(({key, label}) => {
        const opt = document.createElement('option');
        opt.value = key;
        opt.text = label;
        if (key === channel) opt.selected = true;
        select.add(opt);
    });
    lineNode.appendChild(select);

    // Value display span
    res.span = document.createElement('SPAN');
    addAttributes(res.span, {id: 'data_val_' + id, class: 'data_value'});
    lineNode.appendChild(res.span);

    // Strip chart + sparkline SVG
    const stripChart = document.createElement('div');
    stripChart.setAttribute('class', 'strip');
    lineNode.appendChild(stripChart);

    res.sparkline = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    addAttributes(res.sparkline, {'id': 'sparkline_' + id, 'class': 'sparkline'});
    stripChart.appendChild(res.sparkline);

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.setAttribute('class', 'data-remove');
    removeBtn.textContent = '×';
    lineNode.appendChild(removeBtn);

    select.addEventListener('change', () => {
        res.channel = select.value;
        onChange(select.value);
    });

    removeBtn.addEventListener('click', () => onRemove());

    return res;
}

/**
 * Adds an "add field" button to element.
 * @param {HTMLElement} element - container to append the button to
 * @param {function()} onClick - called when the button is clicked
 * @returns {HTMLElement} the button element
 */
export function addAddButton(element, onClick) {
    const btn = document.createElement('button');
    btn.setAttribute('class', 'data-add-btn');
    btn.textContent = '+ add field';
    btn.addEventListener('click', onClick);
    element.appendChild(btn);
    return btn;
}

/**
 * Add a map line to element.
 * @param {*} element
 * @param {*} name
 * @returns {*}
 */
export function addMap(element, name) {
    const res = {name: name};
    const mapNode = document.createElement('div');
    mapNode.setAttribute('id', name+'_div');
    mapNode.setAttribute('class', name+'_class');
    element.appendChild(mapNode);

    // add an SVG to show some sparklines
    res.map = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); // document.createElement("SVG");
    addAttributes(res.map, {'id': 'map', 'class': 'map',
        'width': 250, 'height': 250, 'stroke-width': 3});
    mapNode.appendChild(res.map);

    return res;
}
