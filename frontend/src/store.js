import{api}from'./api.js';
export const state={user:null,data:null,queue:null};
export async function refresh(){[state.data,state.queue]=await Promise.all([api('/archive'),api('/moisture-queue')]);return state}
