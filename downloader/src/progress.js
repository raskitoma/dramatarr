'use strict';
function createProgressReporter(send) {
  let total=0, completed=0, failed=0, completedBytes=0, episode=null, currentBytes=0, currentTotal=null, percentage=null;
  return event => {
    if (event.total !== undefined) total=Number(event.total)||total;
    if (event.completed !== undefined) completed=Number(event.completed)||0;
    if (event.episode !== undefined && event.episode !== episode) {episode=event.episode;currentBytes=0;currentTotal=null;percentage=null;}
    if (event.stage === 'downloading') {currentBytes=0;currentTotal=null;percentage=null;}
    if (event.stage === 'episode_error') failed++;
    if (event.stage === 'episode_done' || event.stage === 'episode_skipped') {
      completedBytes+=Number(event.bytes)||0;currentBytes=0;percentage=100;
    } else if (event.bytes !== undefined) currentBytes=Number(event.bytes)||0;
    if (event.totalBytes) currentTotal=Number(event.totalBytes);
    if (event.percentage !== undefined) percentage=Number(event.percentage);
    send({kind:'progress',stage:event.stage,message:event.message,
      episodesTotal:total,episodesCompleted:completed,episodesFailed:failed,currentEpisode:episode,
      downloadedBytes:completedBytes+currentBytes,currentEpisodeBytes:currentBytes,
      currentEpisodeTotalBytes:currentTotal,currentEpisodePercent:percentage,
      downloadedSegments:event.downloadedSegments,totalSegments:event.totalSegments});
  };
}
module.exports={createProgressReporter};
