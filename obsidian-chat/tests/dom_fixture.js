// DOM operations used by the chat integration fixtures. Rendering still runs
// through the real chat scripts; browser smoke checks cover layout and parsing.
function createElement(tag = 'div') {
  const element = {
    tagName: String(tag).toUpperCase(), children: [], parentElement: null,
    dataset: {}, style: {}, className: '', value: '', hidden: true,
    disabled: false, scrollHeight: 1000, scrollTop: 0, clientHeight: 600,
    _html: '', _text: '', _outer: '',
    get isConnected() { return Boolean(this.parentElement); },
    get firstElementChild() { return this.children[0] || null; },
    get nextElementSibling() {
      const siblings = this.parentElement?.children || [];
      return siblings[siblings.indexOf(this) + 1] || null;
    },
    getBoundingClientRect() { return { top: 0, bottom: 600, width: 400, height: 600 }; },
    addEventListener() {},
    setAttribute(key, value) { this[key] = String(value); },
    contains(other) { return this === other || this.children.some(child => child.contains(other)); },
    remove() {
      if (this.parentElement) {
        const siblings = this.parentElement.children;
        siblings.splice(siblings.indexOf(this), 1);
        this.parentElement = null;
      }
    },
    replaceWith(other) {
      const parent = this.parentElement;
      if (parent) { parent.insertBefore(other, this); this.remove(); }
    },
    insertBefore(child, reference) {
      if (!child) throw new TypeError('Missing child');
      if (reference && reference.parentElement !== this) throw new Error('Foreign reference');
      child.remove();
      const index = reference ? this.children.indexOf(reference) : this.children.length;
      this.children.splice(index, 0, child);
      child.parentElement = this;
    },
    appendChild(child) { this.insertBefore(child, null); return child; },
    prepend(child) { this.insertBefore(child, this.firstElementChild); },
    replaceChildren(...children) {
      for (const child of this.children) child.parentElement = null;
      this.children = [];
      this._html = this._text = '';
      for (const child of children) this.appendChild(child);
    },
    querySelector(selector) {
      if (selector.startsWith(':scope > .')) {
        const className = selector.slice(':scope > .'.length);
        return this.children.find(child => child.classList.contains(className)) || null;
      }
      return null;
    },
    querySelectorAll() { return this.children; },
    get outerHTML() { return this._outer || this.innerHTML; },
    get innerHTML() {
      if (this._text) return this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      return this._html + this.children.map(child => child.outerHTML).join('');
    },
    set innerHTML(value) {
      this.replaceChildren();
      this._html = String(value);
      if (this.tagName === 'TEMPLATE') {
        const root = createElement();
        root._outer = this._html;
        root.id = /\bid="([^"]+)"/.exec(this._html)?.[1] || '';
        root.className = /\bclass="([^"]+)"/.exec(this._html)?.[1] || '';
        root.dataset.createdAt = /data-created-at="([^"]+)"/.exec(this._html)?.[1];
        this.content = { firstElementChild: root };
      }
    },
    get textContent() { return this._text; },
    set textContent(value) { this.replaceChildren(); this._text = String(value); },
  };
  element.classList = {
    contains: name => element.className.split(/\s+/).includes(name),
    add: name => { if (!element.classList.contains(name)) element.className += ' ' + name; },
    remove: name => { element.className = element.className.split(/\s+/).filter(x => x !== name).join(' '); },
  };
  return element;
}

function findById(root, id) {
  if (root.id === id) return root;
  for (const child of root.children) {
    const found = findById(child, id);
    if (found) return found;
  }
  return null;
}

module.exports = { createElement, findById };
