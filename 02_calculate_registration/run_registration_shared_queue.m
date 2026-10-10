function run_registration_shared_queue(tissue_root, dry_run)
% RUN_REGISTRATION_SHARED_QUEUE Run independent 2x CODA jobs on many computers.
% Call the same function on every worker, pointing to the same SMB/UNC root.
% A claim directory is created atomically for each slide. Claims are permanent:
% failed or interrupted jobs need manual review before they can be retried.
%
% run_registration_shared_queue('\\server\share\Tissue types', true)  % list
% run_registration_shared_queue('\\server\share\Tissue types')        % run
%
% Do not run the older uncoordinated batch runner at the same time.

if nargin < 2; dry_run = false; end
assert(isfolder(tissue_root), 'Tissue root is unavailable.');
assert(isscalar(dry_run) && (islogical(dry_run) || isnumeric(dry_run)), ...
    'dry_run must be a scalar logical value.');
if ~dry_run
    assert(license('test', 'image_toolbox'), 'Image Processing Toolbox is required.');
    assert(usejava('jvm'), 'A JVM is required for atomic directory claims.');
end

queue_root = fullfile(tissue_root, 'batch_registration_queue');
claims_root = fullfile(queue_root, 'claims');
if ~dry_run
    if ~isfolder(queue_root); mkdir(queue_root); end
    if ~isfolder(claims_root); mkdir(claims_root); end
    assert(isfolder(claims_root), 'Cannot create the shared claims directory.');
end

dirs = dir(tissue_root);
dirs = dirs([dirs.isdir]);
ids = string({dirs.name})';
ids = ids(~ismember(ids, [".", "..", "batch_registration_logs", ...
    "batch_registration_queue"]));
ids = ids(~startsWith(ids, '.'));
host = string(getenv('COMPUTERNAME'));
if strlength(host) == 0; host = "unknown-host"; end
host = regexprep(host, '[^A-Za-z0-9_-]', '_');
worker = host + "_" + string(feature('getpid'));
fprintf('Worker %s: %d tissue folders; dry_run=%d\n', worker, numel(ids), dry_run);
if ~dry_run
    rng('shuffle');
    ids = ids(randperm(numel(ids)));
end

for k = 1:numel(ids)
    sid = ids(k);
    folder = fullfile(tissue_root, char(sid), '2x');
    [eligible, reason] = quick_check(folder, sid);
    if ~eligible
        fprintf('%s: skipped (%s)\n', sid, reason);
        continue
    end
    if dry_run
        fprintf('%s: eligible\n', sid);
        continue
    end

    % File.mkdir() maps to one directory-create operation on the SMB server.
    % Exactly one worker receives true for this slide's new claim directory.
    claim = fullfile(claims_root, char(sid));
    claim_dir = java.io.File(char(claim));
    claimed = claim_dir.mkdir();
    if ~claimed
        if ~isfolder(claim)
            error('Could not claim %s; the shared queue may be unavailable.', sid);
        end
        fprintf('%s: already claimed\n', sid);
        continue
    end

    started = datetime('now', 'TimeZone', 'UTC');
    write_status(claim, sid, worker, "running", "", started);
    fprintf('%s: claimed by %s at %s UTC\n', sid, worker, char(started));
    try
        % An older, uncoordinated process might have written outputs after
        % the first check. Never run over those outputs.
        [eligible, reason] = quick_check(folder, sid);
        assert(eligible, 'Slide changed after claim: %s', reason);
        [files, ref, expected, suffix] = validate_inputs(folder, sid);
        image = string({files.name})';
        scanner = strings(8, 1);
        for j = 1:8
            scanner(j) = suffix(find(expected == image(j), 1));
        end
        manifest = fullfile(claim, 'scanners.csv');
        writetable(table(image, scanner), manifest);
        rng(20261008, 'twister');
        run_registration(folder, 0, ref, [], 'mixed', manifest, 5);
        verify_outputs(folder, files, ref);
        status = "computed_pending_visual_qc";
        detail = "S210 anchor; 5 um/pixel; H&E; automatic CODA masks";
    catch failure
        status = "failed_review_required";
        detail = string(failure.message);
        fprintf(2, '%s: %s\n', sid, detail);
    end
    write_status(claim, sid, worker, status, detail, started);
    fprintf('%s: %s\n', sid, status);
end
fprintf('Worker %s finished scanning the shared queue.\n', worker);
end

function [ok, reason] = quick_check(folder, sid)
ok = false;
reason = "";
ihc = "Hum Liv PDAC " + ["CD1A", "CD20", "CD68", "CD163", ...
    "CK8", "Collagen IV", "Ki-67", "vimentin"];
if ismember(sid, ihc); reason = "IHC slide"; return; end
if ~isfolder(folder); reason = "no 2x folder"; return; end
if isfolder(fullfile(folder, 'registered')) || isfolder(fullfile(folder, 'TA'))
    reason = "existing or partial registration output"; return
end
suffix = ["S210", "S360", "Leica", "Olympus", "P1000", ...
    "Pramana", "Roche", "Zeiss"];
expected = sid + "_" + suffix + ".tif";
files = dir(fullfile(folder, '*.tif'));
if numel(files) ~= 8 || ~isequal(sort(string({files.name})), sort(expected))
    reason = "not exactly eight expected scanner TIFFs"; return
end
other = dir(folder); other = other(~[other.isdir]);
if ~all(ismember(string({other.name}), expected))
    reason = "unexpected or partial input file"; return
end
ok = true;
end

function [files, ref, expected, suffix] = validate_inputs(folder, sid)
suffix = ["S210", "S360", "Leica", "Olympus", "P1000", ...
    "Pramana", "Roche", "Zeiss"];
expected = sid + "_" + suffix + ".tif";
files = dir(fullfile(folder, '*.tif'));
assert(numel(files) == 8 && isequal(sort(string({files.name})), sort(expected)), ...
    'Input TIFF set changed.');
before = files;
for j = 1:8
    info = imfinfo(fullfile(folder, files(j).name));
    assert(numel(info) == 1, 'Expected single-page TIFF: %s', files(j).name);
    unit = lower(string(info.ResolutionUnit));
    if unit == "centimeter"; scale = 10000;
    elseif unit == "inch"; scale = 25400;
    else; error('Missing physical resolution unit: %s', files(j).name);
    end
    assert(abs(scale / info.XResolution - 5) < 0.01 && ...
        abs(scale / info.YResolution - 5) < 0.01, ...
        'Expected 5 um/pixel: %s', files(j).name);
    pixels = imread(fullfile(folder, files(j).name));
    assert(ndims(pixels) == 3 && size(pixels, 3) == 3, ...
        'Expected RGB TIFF: %s', files(j).name);
    clear pixels
end
after = dir(fullfile(folder, '*.tif'));
assert(isequal({before.name}, {after.name}) && ...
    isequal([before.bytes], [after.bytes]) && ...
    isequal([before.datenum], [after.datenum]), 'Input TIFFs changed during validation.');
ref = find(string({files.name}) == sid + "_S210.tif");
assert(isscalar(ref), 'S210 reference is missing.');
end

function verify_outputs(folder, files, ref)
warps = fullfile(folder, 'registered', 'elastic registration', 'save_warps');
for j = 1:numel(files)
    [~, stem] = fileparts(files(j).name);
    assert(isfile(fullfile(warps, [stem, '.mat'])), ...
        'Missing global/reference transform: %s', stem);
    if j ~= ref
        assert(isfile(fullfile(warps, 'D', [stem, '.mat'])), ...
            'Missing elastic transform: %s', stem);
    end
end
end

function write_status(claim, sid, worker, status, detail, started)
slide_id = sid;
started_utc = string(started);
updated_utc = string(datetime('now', 'TimeZone', 'UTC'));
result = table(slide_id, worker, status, detail, started_utc, updated_utc);
writetable(result, fullfile(claim, 'status.csv'));
end
