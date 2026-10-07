import type { Metadata } from "next";
import { SessionProvider } from "@/lib/auth/session";

import "./globals.css";

export const metadata: Metadata = {
  title: "Atlas AI",
  description: "Haz preguntas sobre tus documentos y obtén respuestas con citas.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="es">
      <body>
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
