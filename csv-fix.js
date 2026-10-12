(function(){
  window.saveCsvFile=async function(csvText,filename){
    const fullText="\uFEFF"+String(csvText||"");

    // Prefer the browser's native Save As dialog where supported.
    if(window.showSaveFilePicker){
      try{
        const handle=await window.showSaveFilePicker({
          suggestedName:filename,
          types:[{description:"CSV file",accept:{"text/csv":[".csv"]}}]
        });
        const writable=await handle.createWritable();
        await writable.write(new Blob([fullText],{type:"text/csv;charset=utf-8"}));
        await writable.close();
        return true;
      }catch(e){
        console.warn("Native save picker unavailable or cancelled",e);
      }
    }

    // Normal Chrome/Android one-tap download when OpsGuard is top-level.
    try{
      const blob=new Blob([fullText],{type:"text/csv;charset=utf-8"});
      const url=URL.createObjectURL(blob);
      const a=document.createElement("a");
      a.href=url;
      a.download=filename;
      a.style.display="none";
      document.body.appendChild(a);
      a.click();
      setTimeout(function(){
        try{URL.revokeObjectURL(url);}catch(e){}
        try{a.remove();}catch(e){}
      },10000);
      return true;
    }catch(e){
      console.warn("Blob download failed",e);
    }

    // Final browser fallback.
    try{
      const a=document.createElement("a");
      a.href="data:text/csv;charset=utf-8,"+encodeURIComponent(fullText);
      a.download=filename;
      a.style.display="none";
      document.body.appendChild(a);
      a.click();
      a.remove();
      return true;
    }catch(e){
      console.warn("Data URL download failed",e);
      return false;
    }
  };

  window.exportAdminCsvDownload=async function(){
    const csvStatus=document.getElementById("csv-status");
    if(!authToken){alert("Login first.");return;}

    if(csvStatus)csvStatus.textContent="Preparing export...";
    try{
      if(!adminFilteredShifts||!adminFilteredShifts.length){
        await loadAllShifts();
      }

      const csvText=buildCsvFromLoadedRows();
      const today=new Date().toISOString().slice(0,10);
      const filename="OpsGuard_Shifts_Export_"+today+".csv";

      // Keep the proven view/copy fallbacks visible, but do not open any extra tab.
      createCsvDownloadLinks(csvText,filename,false);

      const ok=await window.saveCsvFile(csvText,filename);
      if(csvStatus){
        csvStatus.textContent=ok
          ?"CSV download triggered. Check Chrome Downloads. If it does not appear, use Open CSV in new tab or Show / Copy CSV below."
          :"Chrome blocked the download. Use Open CSV in new tab or Show / Copy CSV below.";
      }
    }catch(e){
      const message=e&&e.message?e.message:String(e);
      if(csvStatus)csvStatus.textContent="Export failed: "+message;
      alert("Export failed: "+message);
    }
  };
})();