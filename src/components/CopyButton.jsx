import { useEffect, useRef, useState } from 'react';
import { CheckIcon, CopyIcon } from './Icons.jsx';

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.className = 'copy-buffer';
  document.body.appendChild(area);
  area.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(area);
  if (!ok) throw new Error('Copy failed');
}

export default function CopyButton({ text, label = 'Copy', copiedLabel = 'Copied' }) {
  const [state, setState] = useState('idle');
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  const onClick = async () => {
    try {
      await copyText(text);
      setState('copied');
    } catch {
      setState('failed');
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState('idle'), 2000);
  };

  return (
    <button type="button" className="button button-small" onClick={onClick} disabled={!text}>
      {state === 'copied' ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
      <span aria-live="polite">{state === 'copied' ? copiedLabel : state === 'failed' ? 'Select and copy' : label}</span>
    </button>
  );
}
