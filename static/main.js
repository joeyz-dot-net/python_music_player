(() => {
	let ctx = {tree:{}, musicDir:''};
	try { ctx = JSON.parse(document.getElementById('boot-data').textContent); } catch(e) { console.warn('Boot data parse error', e); }
	const ROOT = document.getElementById('tree');
	function el(tag, cls, text){ const e=document.createElement(tag); if(cls)e.className=cls; if(text) e.textContent=text; return e; }

	function buildNode(node){
		const li = el('li','dir');
		const label = el('div','label');
		const arrow = el('span','arrow','▶');
		const nameSpan = el('span','name', node.rel? node.name : '根目录');
		label.appendChild(arrow); label.appendChild(nameSpan);
		label.onclick = () => li.classList.toggle('collapsed');
		li.appendChild(label);
		const ul = el('ul');
		(node.dirs||[]).forEach(d=>ul.appendChild(buildNode(d)));
		(node.files||[]).forEach(f=>{
			const fi = el('li','file',f.name);
			fi.onclick = () => play(f.rel, fi);
			ul.appendChild(fi);
		});
		li.appendChild(ul);
		if(node.rel) li.classList.add('collapsed');
		return li;
	}

	function render(){
		ROOT.innerHTML='';
		const topUL = el('ul');
		// 展开根的子节点
		(ctx.tree.dirs||[]).forEach(d=>topUL.appendChild(buildNode(d)));
		(ctx.tree.files||[]).forEach(f=>{
			const fi = el('li','file',f.name); fi.onclick=()=>play(f.rel,fi); topUL.appendChild(fi);
		});
		ROOT.appendChild(topUL);
	}

	function play(rel, dom){
		fetch('/play', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'path='+encodeURIComponent(rel)})
			.then(r=>r.json())
			.then(j=>{
				if(j.status!=='OK') { alert('播放失败: '+ j.error); return; }
				document.querySelectorAll('.file.playing').forEach(e=>e.classList.remove('playing'));
				dom.classList.add('playing');
				document.getElementById('status').textContent = '播放: '+ rel;
			}).catch(e=>alert('请求错误: '+ e));
	}

	document.getElementById('expandAll').onclick=()=>document.querySelectorAll('#tree .dir').forEach(d=>d.classList.remove('collapsed'));
	document.getElementById('collapseAll').onclick=()=>document.querySelectorAll('#tree .dir').forEach(d=>d.classList.add('collapsed'));
	render();
})();
