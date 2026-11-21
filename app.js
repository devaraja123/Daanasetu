function toggleNgo(){
  const role=document.getElementById('role');
  const fields=document.getElementById('ngo-fields');
  if(!role||!fields)return;
  const show=role.value==='ngo'; fields.style.display=show?'flex':'none';
  fields.querySelectorAll('input').forEach(el=>el.required=show && (el.name==='organization_name'||el.name==='registration_no'));
}
document.addEventListener('DOMContentLoaded',toggleNgo);

function captureLocation(){
 const status=document.getElementById('gps-status');
 if(!navigator.geolocation){status.textContent='Geolocation is not supported by this browser.';return;}
 status.textContent='Requesting permission…';
 navigator.geolocation.getCurrentPosition(pos=>{
  document.getElementById('latitude').value=pos.coords.latitude.toFixed(7);
  document.getElementById('longitude').value=pos.coords.longitude.toFixed(7);
  status.textContent='Location captured: '+pos.coords.latitude.toFixed(5)+', '+pos.coords.longitude.toFixed(5);
 },err=>{status.textContent='Could not get location: '+err.message+' (allow location permission and use localhost/HTTPS).';},{enableHighAccuracy:true,timeout:12000,maximumAge:60000});
}
