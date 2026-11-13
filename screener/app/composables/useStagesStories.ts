// stockey story scores, looked up by symbol, plus the median that the fundamentals check uses.
// A failed fetch leaves every stock at "No score" rather than breaking the page.
export function useStagesStories() {
  const { data, error } = useStockey<StagesStoryRow[]>('/api/story-scores')
  const bySymbol = computed(() => new Map((data.value ?? []).map(s => [s.symbol, s])))
  const median = computed(() => stagesMedian((data.value ?? []).map(s => s.story_score).filter((v): v is number => v !== null)))
  const filterOf = (symbol: string) => stagesStoryFilter(bySymbol.value.get(symbol), median.value)
  return { stories: data, storiesError: error, bySymbol, median, filterOf }
}
