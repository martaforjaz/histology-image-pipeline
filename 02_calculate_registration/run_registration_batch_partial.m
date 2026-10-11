function results = run_registration_batch_partial(tissue_root, selection_csv, execute)
% Register slides with 4-7 known scanner TIFFs and an explicit S210 anchor.
% IHC-labelled slides use CODA's IHC=1 mode; all others use H&E mode.
% A fixed selection CSV prevents overlap with the eight-scanner workers.
if nargin<3; execute=false; end
assert(isfolder(tissue_root),'Tissue root does not exist');
assert(isfile(selection_csv),'A fixed selection CSV is required');
selected=readtable(selection_csv,'TextType','string','Delimiter',',','VariableNamingRule','preserve');
assert(ismember('slide_id',selected.Properties.VariableNames),'Missing slide_id column');
ids=selected.slide_id;
assert(numel(unique(ids))==numel(ids),'Duplicate slide IDs');
suffix=["S210","S360","Leica","Olympus","P1000","Pramana","Roche","Zeiss"];
% Unique across computers sharing the same NAS audit directory.
stamp=[char(datetime('now','Format','yyyyMMdd_HHmmss_SSS')),'_', ...
    char(javaMethod('randomUUID','java.util.UUID'))];
results=table('Size',[0 3],'VariableTypes',{'string','string','string'}, ...
    'VariableNames',{'slide_id','status','detail'});
if execute
    assert(license('test','image_toolbox'),'Image Processing Toolbox required');
    audit=fullfile(tissue_root,'batch_registration_logs');
    if ~isfolder(audit);mkdir(audit);end
    logfile=fullfile(audit,['batch_',stamp,'.csv']);
end
for k=1:numel(ids)
    sid=ids(k);status="skipped";detail="";
    try
        assert(~contains(sid,["/","\",".."]),'Invalid slide directory name');
        ihc_ids="Hum Liv PDAC "+["CD1A","CD20","CD68","CD163","CK8","Collagen IV","Ki-67","vimentin"];
        ihc=double(ismember(sid,ihc_ids));
        folder=fullfile(tissue_root,char(sid),'2x');
        expected=sid+"_"+suffix+".tif";
        ff=dir(fullfile(folder,'*.tif'));
        names=string({ff.name});
        assert(numel(ff)>=4 && numel(ff)<=7 && numel(unique(names))==numel(ff) && ...
            all(ismember(names,expected)),'Need 4-7 unique known scanner TIFFs');
        assert(any(names==sid+"_S210.tif"),'S210 reference TIFF is required');
        other=dir(folder);other=other(~[other.isdir]);
        assert(all(ismember(string({other.name}),names)), ...
            'Unexpected/partial files in input folder');
        assert(~isfolder(fullfile(folder,'registered')) && ~isfolder(fullfile(folder,'TA')), ...
            'Existing registration/masks: review provenance or use a separate working copy');
        before=ff;
        for j=1:numel(ff)
            p=fullfile(folder,ff(j).name);info=imfinfo(p);
            assert(numel(info)==1,'Expected single-page 2x TIFF');
            unit=lower(string(info.ResolutionUnit));
            if unit=="centimeter";scale=10000;
            elseif unit=="inch";scale=25400;
            else;error('Missing physical resolution units: %s',ff(j).name);end
            assert(abs(scale/info.XResolution-5)<.01 && abs(scale/info.YResolution-5)<.01, ...
                'Expected 5 um/pixel in both axes');
            im=imread(p);assert(ndims(im)==3 && size(im,3)==3,'Expected RGB');clear im
        end
        after=dir(fullfile(folder,'*.tif'));
        assert(isequal({before.name},{after.name}) && isequal([before.bytes],[after.bytes]) ...
            && isequal([before.datenum],[after.datenum]),'Inputs changed during validation');
        ref=find(string({ff.name})==sid+"_S210.tif");assert(isscalar(ref));
        if execute
            % Record configuration outside input folder so eligibility stays strict.
            image=string({ff.name})';scanner=strings(numel(ff),1);
            for j=1:numel(ff)
                q=find(expected==image(j));scanner(j)=suffix(q);
            end
            manifest=fullfile(audit,char(sid+"_"+stamp+"_scanners.csv"));
            writetable(table(image,scanner),manifest);
            rng(20261008,'twister');
            run_registration(folder,ihc,ref,[], 'mixed',manifest,5);
            warps=fullfile(folder,'registered','elastic registration','save_warps');
            for j=1:numel(ff)
                [~,stem]=fileparts(ff(j).name);
                assert(isfile(fullfile(warps,[stem,'.mat'])),'Missing global/reference output');
                if j~=ref
                    assert(isfile(fullfile(warps,'D',[stem,'.mat'])),'Missing elastic output');
                end
            end
            status="computed_pending_visual_qc";
        else
            status="ready";
        end
        mode="H&E";if ihc==1;mode="IHC";end
        detail="S210 anchor; 5 um/pixel; "+mode+"; "+numel(ff)+" scanners; automatic CODA tissue masks";
    catch failure
        detail=string(failure.message);
        if execute;status="skipped_or_failed";end
    end
    results(end+1,:)={sid,status,detail}; %#ok<AGROW>
    fprintf('%s: %s — %s\n',sid,status,detail);
    if execute;writetable(results,logfile);end
end
end
