import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post, type Movies, type Overview, type Player } from "./api";
import { useToast } from "./toast";

/** Pairing requests and what's playing change on their own, so this keeps checking. */
export const useOverview = () =>
  useQuery({ queryKey: ["overview"], queryFn: () => get<Overview>("overview"), refetchInterval: 2000 });

export const useMovies = (collection: string) =>
  useQuery({
    queryKey: ["movies", collection],
    queryFn: () => get<Movies>(`movies${collection ? `?collection=${encodeURIComponent(collection)}` : ""}`),
    staleTime: 5 * 60_000,
  });

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
