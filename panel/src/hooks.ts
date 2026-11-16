import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post, type CardsState, type Filters, type Movies, type Overview, type Player } from "./api";
import { useToast } from "./toast";

/** Pairing requests and what's playing change on their own, so this keeps checking. */
export const useOverview = () =>
  useQuery({ queryKey: ["overview"], queryFn: () => get<Overview>("overview"), refetchInterval: 2000 });

export const useMovies = (collection: string, label = "") =>
  useQuery({
    queryKey: ["movies", collection, label],
    queryFn: () => {
      const query = new URLSearchParams();
      if (collection) query.set("collection", collection);
      if (label) query.set("label", label);
      return get<Movies>(`movies${query.size ? `?${query}` : ""}`);
    },
    staleTime: 5 * 60_000,
  });

export const useFilters = () =>
  useQuery({ queryKey: ["filters"], queryFn: () => get<Filters>("filters"), staleTime: 5 * 60_000 });

/** Linked cards and the scan in progress: checked every second while a scan is going. */
export const useCards = () =>
  useQuery({
    queryKey: ["cards"],
    queryFn: () => get<CardsState>("cards"),
    refetchInterval: (q) => (q.state.data?.scan ? 1000 : 3000),
  });

/** A POST to the card routes, whose answer is the new card state. */
export function useCardAction<V>(path: (v: V) => string, body: (v: V) => object = () => ({})) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (v: V) => post<CardsState & { ok: boolean }>(path(v), body(v)),
    onSuccess: (state) => {
      client.setQueryData(["cards"], state);
      client.invalidateQueries({ queryKey: ["movies"] });
    },
    onError: (error) => toast(error.message, "error"),
  });
}

/** Every player a movie can go to: asks Plex and Jellyfin, so it's fetched less often. */
export const usePlayers = () =>
  useQuery({
    queryKey: ["players"],
    queryFn: () => get<{ players: Player[]; default: string }>("players"),
    staleTime: 60_000,
  });

/** A POST to the panel API that refreshes the overview afterwards and reports how it went. */
export function useAction<V>(path: (v: V) => string, body: (v: V) => object = () => ({}),
                             success?: (v: V, result: { message?: string }) => string | undefined) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (v: V) => post(path(v), body(v)),
    onSuccess: (result, v) => {
      const text = success?.(v, result);
      if (text) toast(text);
    },
    onError: (error) => toast(error.message, "error"),
    onSettled: () => client.invalidateQueries({ queryKey: ["overview"] }),
  });
}
