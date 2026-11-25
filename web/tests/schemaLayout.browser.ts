/** Browser-side layout checks for the schema explorer; run in a page served by Vite (the page supplies the CSS). */
export function runSchemaLayoutChecks(): string[] {
  const results: string[] = [];
  const check = (condition: boolean, name: string) => { if (!condition) throw new Error(name); results.push(name); };
  const longType = "enum('draft','active','unpublished','archived','deleted','pending_review','scheduled','rejected')";
  const host = document.createElement('div');
  host.className = 'sc-page';
  host.style.cssText = 'position:fixed;left:-9999px;top:0;visibility:hidden;overflow:visible;';
  host.innerHTML = `
    <div class="sc-fields" style="width:300px"><div class="sc-field"><p class="sc-field-top"><span class="sc-field-name">status</span><span class="sc-field-type">${longType}</span></p>
      <p class="sc-field-tags"><span class="sc-tag">default draft</span><span class="sc-tag">FK → ${'very_long_table_name_'.repeat(4)}.id</span></p></div></div>
    <div class="sc-cols" style="width:240px"><span class="sc-col"><span class="sc-col-name">status</span><span class="sc-col-type">${longType}</span></span></div>`;
  document.body.appendChild(host);
  try {
    const lineHeight = (element: Element) => parseFloat(getComputedStyle(element).lineHeight) || parseFloat(getComputedStyle(element).fontSize) * 1.4;
    const name = host.querySelector('.sc-field-name') as HTMLElement;
    const type = host.querySelector('.sc-field-type') as HTMLElement;
    const box = host.querySelector('.sc-fields') as HTMLElement;
    check(name.getBoundingClientRect().height < lineHeight(name) * 1.6, 'a long type never wraps the column name letter by letter in the table details');
    check(name.getBoundingClientRect().width >= 30, 'the column name keeps a readable width next to a long type');
    check(type.getBoundingClientRect().right <= box.getBoundingClientRect().right + 1, 'a long type stays inside the details panel');
    for (const tag of host.querySelectorAll('.sc-field-tags .sc-tag')) check(tag.getBoundingClientRect().right <= box.getBoundingClientRect().right + 1, 'field tags stay inside the details panel');
    const cardName = host.querySelector('.sc-col-name') as HTMLElement;
    const cardRow = host.querySelector('.sc-col') as HTMLElement;
    check(cardName.getBoundingClientRect().width >= 30, 'the column name keeps room next to a long type in table cards');
    check(cardRow.scrollWidth <= cardRow.clientWidth + 1, 'a long type does not overflow a table card');
  } finally { host.remove(); }
  return results;
}
