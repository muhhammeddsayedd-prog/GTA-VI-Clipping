import type { Metadata } from "next";

import { PostsView } from "./PostsView";

export const metadata: Metadata = { title: "Posts" };

export default function PostsPage() {
  return <PostsView />;
}
