// One vector master supplies every brand placement and favicon derivative.
export const brandSource = '/brand.svg';
export function brandImage(size=36,decorative=false) {
  const image=document.createElement('img');image.src=brandSource;image.width=size;image.height=size;image.alt=decorative?'':'LoopCast';image.className='brand-image';return image;
}
